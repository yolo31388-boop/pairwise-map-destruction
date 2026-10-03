# 地图破坏系统

## 项目描述

地图破坏系统的bug修复项目。核心模块 `map/destruction.py` 存在多个已知bug，需要修复后使测试全部通过。

## 已知问题

- 伤害只减血量不检查破坏阈值
- 墙壁破坏后碰撞体还在玩家不能通过
- 碎片不应用力无碰撞数量不限
- 结构完整性不检查支撑关系
- 破坏效果只有粒子无裂痕变形

## 项目结构

```
├── map/destruction.py       # 核心模块（带bug）
├── tests/test_map_destruction.py    # 验收测试（红态）
├── pyproject.toml          # 项目配置
└── README.md               # 说明文档
```

## 运行测试

```bash
python -m pytest tests/test_map_destruction.py -q
```

修复目标：所有 15 个测试全部通过。

## 技术要求

- 纯Python标准库，不引入第三方依赖
- 不修改测试文件
- 保持现有API接口不变
- 修复所有已知bug
