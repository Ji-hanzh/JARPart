# JARPart 核心源码

JARPart 面向具有固定节点、容量与最大通信跳数约束的多 FPGA 划分，提供子社区构建、联合分配与辅助可行性恢复。

- `core/subcommunity.py`：按社区与当前 FPGA 归属构建子社区。
- `core/joint_assignment.py`：顺序调整和联合分配。
- `core/feasibility_recovery.py`：局部恢复及联合恢复。
- `core/_engine/`：实际的图聚合、目标计算、CP-SAT 模型和运行控制。
- `examples/`：人工构造的小样例。
- `tests/`：约束、联合交换、超时与回退测试。
- `config/`、`tools/`：真实电路社区缓存的校验与安装。

安装与运行命令见 [README.md](README.md)，输入格式见 [docs/INTERFACE.md](docs/INTERFACE.md)。
使用 Linux 或 WSL，安装 `requirements.txt` 中的依赖后，通过 `python run.py --help` 查看入口。

真实电路社区缓存作为 GitHub Release 附件提供，包含 30 个电路的 Fixed/Non-Fixed 共 60 份社区编号。
缓存需要与输入图和节点顺序匹配。
