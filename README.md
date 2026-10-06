# pairwise-dedup

只依赖 Python 标准库的流式去重窗口内核：到达的刻度由调用方注入，内核不使用
真实时钟、线程、网络或随机数，同一串到达永远得到同一串判定与同一组计数。

- `dedup/core.py` — 内核：滑动时间窗、精确集合与近似结构协同、过期清理、
  乱序到达、容量淘汰、首次刻度与重复度计数。
- `tests/test_core.py` — 验收用例。

## 运行测试

在项目根目录执行：

```
python3 -m unittest discover -s tests -v
```

Windows 上如果没有 `python3`，可用：

```
python -m unittest discover -s tests -v
```
