# VNT 2.0 升级说明

## 更新内容

### 1. VNT CLI 可执行文件更新
- **变更**: `vnt-cli.exe` → `vnt2_cli.exe`
- **位置**: `vnt_daemon.py` 第18行
- **说明**: 使用VNT 2.0版本的命令行工具

### 2. 配置文件格式转换机制
- **新增功能**: YAML到TOML自动转换
- **位置**: `vnt_daemon.py` 中的 `_convert_yaml_to_toml()` 方法
- **工作流程**:
  1. 读取YAML配置文件（保持不变）
  2. 自动转换为TOML格式
  3. 使用 `--conf` 参数传递TOML文件给 vnt2_cli.exe
  
- **关键映射规则**:
  - `token` → `network_code`
  - `server_address` 协议前缀映射:
    - `udp://` → `quic://`
    - `tcp://` → `tcp://`
    - `ws://` → `ws://`
    - `wss://` → `wss://`
  - `compressor: lz4` → `compress: true`
  - `server_encrypt: true` → `cert_mode: "standard"`
  - `server_encrypt: false` → `cert_mode: "skip"`

### 3. GUI界面协议选项更新
- **位置**: `vnt_helper.py` 中的 `VNT_Main_Window` 类
- **变更内容**:
  - 协议选择从 `UDP` 改为 `QUIC (UDP)`
  - 显示名称: `QUIC (UDP)` 体现实际协议为QUIC，底层传输为UDP
  - 内部存储仍使用 `udp://` 以保持YAML配置兼容性

- **修改的方法**:
  1. `__init__()`: 协议选项列表更新
  2. `refresh_ui()`: 国际化刷新时更新协议选项
  3. `_write_vnt_connection_config()`: 保存配置时的协议映射
  4. `_load_settings_to_main_window()`: 加载配置时的协议反向映射
  5. `on_protocol_change()`: 协议选择时的提示信息

### 4. 启动命令更新
- **旧命令**: `vnt-cli.exe -f config.yaml`
- **新命令**: `vnt2_cli.exe --conf config.toml`
- **位置**: `vnt_daemon.py` 的 `start_vnt_cli()` 方法

## 技术细节

### YAML读写机制保持不变
- 所有YAML文件的读写操作保持不变
- `VNT_Config` 类继续使用 `yaml.safe_load()` 和 `yaml.safe_dump()`
- 用户配置文件仍然是 `.yaml` 格式

### TOML转换仅在运行时进行
- YAML → TOML 转换在守护进程启动时自动进行
- 生成的 `.toml` 文件与 `.yaml` 文件同目录
- 每次启动都会重新转换，确保配置同步

### 协议映射规范
根据VNT2官方文档和内存规范：
- UI显示: `QUIC (UDP)` - 用户友好名称
- YAML存储: `udp://` - 保持向后兼容
- TOML生成: `quic://` - VNT2要求的协议格式

## 测试验证

已通过以下测试：
1. ✓ Python语法编译检查
2. ✓ YAML到TOML转换功能测试
3. ✓ 协议映射正确性验证
4. ✓ 字段转换完整性检查

## 注意事项

1. **依赖要求**: 需要安装 `toml` Python库
   ```bash
   pip install toml
   ```

2. **配置文件兼容性**: 
   - 现有的YAML配置文件无需修改
   - 系统会自动处理格式转换

3. **协议选择**:
   - 用户在GUI中选择 `QUIC (UDP)` 时，实际使用的是QUIC协议
   - 这是VNT2推荐的默认协议，提供更好的性能

4. **向后兼容**:
   - YAML配置文件格式保持不变
   - 仅在与vnt2_cli.exe交互时转换为TOML

## 相关文件

- `vnt_daemon.py`: 守护进程，负责YAML→TOML转换和启动vnt2_cli.exe
- `vnt_helper.py`: GUI界面，更新了协议显示和配置保存逻辑
- `vnt2_conf_toml_example.toml`: VNT2 TOML配置文件示例（参考）
- `vnt2_parameters.txt`: VNT2命令行参数说明（参考）
