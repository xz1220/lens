# 把 agent 记忆和向量库混用
_2026-06-08T05:01:00+00:00_
> 触发自 item：[Example: 一篇关于 agent 长期记忆的 paper](https://huggingface.co/papers/0000.00000)

读完那篇 paper 的讨论后冒出来的：把它的「时间戳 + 置信度加权」冲突解决，套在我现有 agent 的
向量库检索之上，可能能解决我一直头疼的旧记忆污染问题。下一步：写个最小原型验证 retrieval 延迟。
