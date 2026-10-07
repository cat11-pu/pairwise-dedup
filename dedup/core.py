"""流式去重窗口内核：注入刻度的滑动时间窗、精确集合与近似结构协同。

时间由调用方以整数刻度注入，内核不读真实时钟、不做 I/O、不联网、不使用
随机数，同一串到达永远给出同一串判定与同一组计数。

窗口两侧协同工作：

* 精确侧：窗口内每个键的出现次数，以及首次被接受时的刻度；
* 近似侧：固定宽度的存在结构，先替精确侧挡掉明显没出现过的键，
  对窗口里还在的键不允许漏报，写满时按窗口里的键重建。

对外接口：

* Observation    —— 一次到达的判定：是否收下、是否重复、是否首次、当前出现次数；
* PresenceSketch —— 近似存在结构；
* DedupWindow    —— 去重窗口内核：观察、推进时间、容量淘汰与累计计数。
"""

import heapq

__all__ = [
    "DedupError",
    "DedupWindow",
    "Observation",
    "PresenceSketch",
    "digest64",
]

MASK64 = (1 << 64) - 1
FNV_OFFSET_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
SKETCH_SALT = 0x9E3779B97F4A7C15


class DedupError(Exception):
    """内核无法完成请求的操作时抛出。"""


def _require_int(value, label):
    """确认参数是非布尔的整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("%s 必须是整数: %r" % (label, value))
    return value


def _require_positive(value, label, minimum=1):
    """确认参数是不小于 minimum 的整数。"""
    _require_int(value, label)
    if value < minimum:
        raise ValueError("%s 不能小于 %d: %r" % (label, minimum, value))
    return value


def _require_fraction(value, label):
    """确认参数是大于 0 且不超过 1 的比例。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("%s 必须是数值: %r" % (label, value))
    if not 0.0 < value <= 1.0:
        raise ValueError("%s 必须落在 0 到 1 之间: %r" % (label, value))
    return float(value)


def _require_key(key):
    """确认键是字符串或字节串。"""
    if isinstance(key, (str, bytes)):
        return key
    raise TypeError("键必须是字符串或字节串: %r" % (key,))


def digest64(key):
    """对键做 64 位 FNV-1a 摘要，与解释器内置散列无关。"""
    key = _require_key(key)
    data = key if isinstance(key, bytes) else key.encode("utf-8")
    digest = FNV_OFFSET_BASIS
    for byte in data:
        digest ^= byte
        digest = (digest * FNV_PRIME) & MASK64
    return digest


def mix64(value):
    """SplitMix64 终混，让相邻的键散到近似结构的不同位置。"""
    value = (value ^ (value >> 30)) * 0xBF58476D1CE4E5B9 & MASK64
    value = (value ^ (value >> 27)) * 0x94D049BB133111EB & MASK64
    return (value ^ (value >> 31)) & MASK64


class Observation:
    """一次到达的判定结果。

    被收下的到达带上重复与首次标记，出现次数按收下之后的窗口计数。
    落在窗口外的到达 accepted 为假，其余标记都是假，出现次数按 0 计。
    """

    __slots__ = ("tick", "key", "accepted", "duplicate", "first",
                 "occurrences", "evicted")

    def __init__(self, tick, key, accepted, duplicate, first,
                 occurrences, evicted):
        self.tick = tick
        self.key = key
        self.accepted = accepted
        self.duplicate = duplicate
        self.first = first
        self.occurrences = occurrences
        self.evicted = evicted

    def __repr__(self):
        return ("Observation(tick=%r, key=%r, accepted=%r, duplicate=%r, "
                "first=%r, occurrences=%r, evicted=%r)"
                % (self.tick, self.key, self.accepted, self.duplicate,
                   self.first, self.occurrences, self.evicted))


class PresenceSketch:
    """固定宽度的近似存在结构。

    对交给 add 的键，maybe_contains 必须回答"见过"；对没见过的键允许
    偶尔也回答"见过"。reset 之后一切从头开始。
    """

    def __init__(self, nbits=256, hashes=2):
        self.nbits = _require_positive(nbits, "sketch_bits", 8)
        self.hashes = _require_positive(hashes, "sketch_hashes")
        self._bytes = bytearray((self.nbits + 7) // 8)
        self._members = 0

    def positions(self, key):
        """一个键占用的位位置。"""
        first = digest64(key)
        second = mix64(first ^ SKETCH_SALT) | 1
        return [(first + step * second) % self.nbits
                for step in range(self.hashes)]

    def add(self, key):
        """记下这个键。"""
        for position in self.positions(key):
            self._bytes[position >> 3] |= 1 << (position & 7)
        self._members += 1

    def maybe_contains(self, key):
        """这个键自从上次 reset 之后是否被记下过。"""
        return all((self._bytes[position >> 3] >> (position & 7)) & 1
                   for position in self.positions(key))

    def reset(self):
        """忘掉此前记下的键，缓冲区留着复用。"""
        for index in range(len(self._bytes)):
            self._bytes[index] = 0
        self._members = 0

    def fill(self):
        """当前置位的比例，落在 0 到 1 之间。"""
        set_bits = sum(bin(value).count("1") for value in self._bytes)
        return set_bits / self.nbits

    def members(self):
        """上次 reset 之后记下的键数。"""
        return self._members


class DedupWindow:
    """滑动去重窗口。

    键在刻度 tick 到达；水标是见过的最晚刻度，只增不减。落在
    (水标 - 窗口长度, 水标] 之内的到达会被收下，更早的到达直接拒绝。
    容量按出现次数计，超出时先淘汰最旧的到达。
    """

    __slots__ = ("_span", "_capacity", "_fill_limit", "_counts", "_first",
                 "_heap", "_sequence", "_watermark", "_sketch", "_arrivals",
                 "_duplicates", "_rejected", "_expired", "_evictions",
                 "_rebuilds")

    def __init__(self, span, capacity, sketch_bits=256, sketch_hashes=2,
                 fill_limit=0.5):
        self._span = _require_positive(span, "窗口长度")
        self._capacity = _require_positive(capacity, "窗口容量")
        self._fill_limit = _require_fraction(fill_limit, "重建阈值")
        self._counts = {}
        self._first = {}
        self._heap = []
        self._sequence = 0
        self._watermark = None
        self._sketch = PresenceSketch(sketch_bits, sketch_hashes)
        self._arrivals = 0
        self._duplicates = 0
        self._rejected = 0
        self._expired = 0
        self._evictions = 0
        self._rebuilds = 0

    # ---- 只读视图 -------------------------------------------------

    @property
    def span(self):
        return self._span

    @property
    def capacity(self):
        return self._capacity

    def watermark(self):
        """已经见过的最晚刻度，还没有到达时为 None。"""
        return self._watermark

    def size(self):
        """窗口里当前保留的出现次数。"""
        return len(self._heap)

    def key_count(self):
        """窗口里当前保留的不同键数。"""
        return len(self._counts)

    def occurrences(self, key):
        """键在窗口里当前的出现次数。"""
        return self._counts.get(_require_key(key), 0)

    def first_tick(self, key):
        """键在窗口里首次被收下的刻度，不在窗口里时为 None。"""
        return self._first.get(_require_key(key))

    def sketch_fill(self):
        """近似结构当前置位的比例。"""
        return self._sketch.fill()

    def stats(self):
        """窗口整个生命周期内的累计计数。"""
        return {
            "arrivals": self._arrivals,
            "duplicates": self._duplicates,
            "rejected": self._rejected,
            "expired": self._expired,
            "evictions": self._evictions,
            "rebuilds": self._rebuilds,
        }

    # ---- 写入 -----------------------------------------------------

    def observe(self, tick, key):
        """收下一次到达，返回它的判定。"""
        tick = _require_int(tick, "时间刻度")
        key = _require_key(key)
        self._advance_watermark(tick)
        self._expire()
        if tick <= self._watermark - self._span:
            self._rejected += 1
            return Observation(tick, key, False, False, False, 0, ())
        return self._accept(tick, key)

    def advance(self, now):
        """把水标推进到 now，返回因此离开窗口的出现次数。"""
        now = _require_int(now, "时间刻度")
        if self._watermark is not None and now < self._watermark:
            raise ValueError("时间只能向前推进: %r" % (now,))
        self._advance_watermark(now)
        return self._expire()

    def seen(self, key):
        """键在窗口里当前是否还有出现。"""
        key = _require_key(key)
        return self._sketch.maybe_contains(key) and key in self._counts

    # ---- 内部结构 -------------------------------------------------

    def _advance_watermark(self, tick):
        """水标是见过的最晚刻度。"""
        if self._watermark is None or tick > self._watermark:
            self._watermark = tick

    def _accept(self, tick, key):
        """收下一条到达并给出判定。"""
        duplicate = self.seen(key)
        first = key not in self._first
        evicted = self._record(tick, key)
        self._arrivals += 1
        if duplicate:
            self._duplicates += 1
        return Observation(tick, key, True, duplicate, first,
                           self._counts.get(key, 0), evicted)

    def _record(self, tick, key):
        """记下一次到达，必要时淘汰与重建。"""
        sequence = self._sequence
        self._sequence += 1
        heapq.heappush(self._heap, (tick, sequence, key))
        self._counts[key] = self._counts.get(key, 0) + 1
        if key not in self._first:
            self._first[key] = tick
        self._sketch.add(key)
        evicted = self._enforce_capacity()
        if self._sketch.fill() > self._fill_limit:
            self._rebuild_sketch()
        return evicted

    def _enforce_capacity(self):
        """按容量淘汰最旧的到达。"""
        evicted = []
        while len(self._heap) > self._capacity:
            evicted.append(self._release())
        self._evictions += len(evicted)
        return tuple(evicted)

    def _release(self):
        """淘汰窗口里最旧的一次出现，返回它的键。"""
        tick, sequence, key = heapq.heappop(self._heap)
        remaining = self._counts[key] - 1
        if remaining:
            self._counts[key] = remaining
        else:
            del self._counts[key]
            self._first.pop(key, None)
        return key

    def _expire(self):
        """丢掉已经离开窗口的出现，返回丢掉的条数。"""
        if self._watermark is None:
            return 0
        dropped = 0
        while self._heap and self._heap[0][0] <= self._watermark - self._span:
            self._release()
            dropped += 1
        self._expired += dropped
        self._prune_sketch(dropped)
        return dropped

    def _prune_sketch(self, dropped):
        """有出现离开窗口后，按窗口里剩下的键重建近似结构。"""
        if dropped:
            self._rebuild_sketch()

    def _rebuild_sketch(self):
        """近似结构写满或有出现离开时重建：清空并重放窗口里还在的键。"""
        self._sketch.reset()
        for key in self._counts:
            self._sketch.add(key)
        self._rebuilds += 1
