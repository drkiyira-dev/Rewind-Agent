# 第三方材料

本目录的 Python 代码是为这次验证新写的。没有复制 Agent VCR、Agent Bisect、ReBaseAgent、Crab、agent-replay、LangGraph 或 Bazel 的源码。

审查时只阅读了这些公开版本，证据等级见 `docs/review.md`：

- ixchio/agent-vcr `5b53389ce9b050041ca3163e34c8f05fb32bbc46`，LICENSE 正文为 MIT。
- kidus-der/agent-bisect `e780460fedbdfad5abe5dee68c008083b2edc3da`，LICENSE 正文为 MIT。
- pomelotea-yuzu/ReBaseAgent `00864e6a9624fcf501c05ef42cfae593b971920c`，LICENSE 正文为 MIT。
- open-agent-infra/crab `9607d61a41dc44358cf078c4b438bfd971c8ee9d`，LICENSE 正文为 MIT。
- clay-good/agent-replay `ccda6229a9451692fb6f1d6d323dd825c2be9dbb`，LICENSE 正文为 MIT。
- langchain-ai/langgraph `f35dc299b390bcaf5188f50a05ca23d148214a86`，LICENSE 正文为 MIT。
- Bazel Skyframe 文档：https://bazel.build/reference/skyframe 。`SkyFunction.java` 文件头为 Apache-2.0，未纳入本目录。

论文摘要只用于核对标题是否存在。论文中的数字没有当作本原型的性能证据。

所有者在本轮交付中选择 GNU GPL 第 3 版。项目采用 `GPL-3.0-only`，完整条款见 `LICENSE`。此前的 MIT 建议未生效；上面的第三方阅读记录保持原样，不代表第三方项目的许可证发生变化。本轮不发布软件包或 Release。
