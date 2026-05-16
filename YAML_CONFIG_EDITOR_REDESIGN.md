# VNT 2.0 YAML 配置编辑器重新设计

## 设计目标

重新设计 [VNT_YamlConfigEditor_Window](file://c:\RussApp\v4\vnt_helper.py#L4671-L5081)，使其能够编辑 VNT 2.0 的**全部配置参数**，同时保持以下架构：

1. **编辑器输出格式**: YAML（保持不变）
2. **转换职责**: vnt_daemon.py 负责将 YAML 转换为 TOML
3. **兼容性**: 其他所有地方的 YAML 逻辑保持不变

## 新设计特点

### 1. 分组表单式布局

采用**折叠面板 + 双列表单**的布局方式，将所有配置项按功能分组：

```
┌─────────────────────────────────────────────┐
│ Network Configuration                       │
│ ├─ Network Code *          [__________]     │
│ ├─ Server Address *        [__________]     │
│ ├─ Virtual IP              [__________]     │
│ ├─ Control Port            [11233      ]     │
│ └─ Tunnel Port             [0         ]     │
├─────────────────────────────────────────────┤
│ Performance Optimization                    │
│ ├─ RTX Optimization         ☑ Enabled      │
│ ├─ FEC Forward Error Corr.  ☐ Enabled      │
│ ├─ LZ4 Compression          ☐ Enabled      │
│ └─ Disable P2P Punch        ☐ Enabled      │
├─────────────────────────────────────────────┤
│ Network Forwarding                          │
│ ├─ Input Networks                           │
│ │  [192.168.0.0/24,10.26.0.2               │
│ │   192.168.1.0/24,10.26.0.3]              │
│ ├─ Output Networks         [0.0.0.0/0]     │
│ └─ Disable Built-in NAT     ☐ Enabled      │
├─────────────────────────────────────────────┤
│ ... (其他分组)                               │
└─────────────────────────────────────────────┘
         [Save and Exit] [Save As...] [Close]
```

### 2. 配置字段定义

使用字典定义所有配置项，便于维护和扩展：

```python
CONFIG_FIELDS = {
    'network_code': {
        'label': _('Network Code') + ' *',
        'type': 'text',
        'required': True,
        'help': _('Network identifier (required)'),
        'section': 'network'
    },
    'server': {
        'label': _('Server Address') + ' *',
        'type': 'text',
        'required': True,
        'help': _('Server address list'),
        'section': 'network',
        'placeholder': 'quic://1.2.3.4:29872'
    },
    # ... 更多字段
}
```

### 3. 支持的字段类型

| 类型 | 控件 | 示例 | 说明 |
|------|------|------|------|
| `text` | wx.TextCtrl | `"quic://1.2.3.4:29872"` | 单行文本 |
| `password` | wx.TextCtrl (TE_PASSWORD) | `"******"` | 密码输入 |
| `int` | wx.SpinCtrl | `11233` | 整数（0-65535） |
| `bool` | wx.CheckBox | `☑ Enabled` | 布尔值 |
| `choice` | wx.Choice | `["skip", "standard"]` | 下拉选择 |
| `text_list` | wx.TextCtrl (TE_MULTILINE) | 多行文本 | 字符串列表 |

### 4. 配置分组

共 7 个功能分组：

1. **Network Configuration** - 网络配置（必填项标红）
2. **Performance Optimization** - 性能优化
3. **Network Forwarding** - 网络转发
4. **Port Mapping** - 端口映射
5. **Device Configuration** - 设备配置
6. **Security Configuration** - 安全配置
7. **STUN Configuration** - STUN 配置

### 5. 数据流

```
用户编辑UI
  ↓
collect_data_from_ui() - 从控件收集数据
  ↓
验证必填字段
  ↓
save_yaml() - 保存为YAML格式
  ↓
vnt_daemon.py 读取YAML
  ↓
yaml_to_toml_converter.convert()
  ↓
生成TOML配置文件
  ↓
vnt2_cli.exe 读取TOML
```

## 完整的配置字段列表

### Network Configuration（网络配置）

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| network_code | text | ✅ | - | 网络编号（必填） |
| server | text | ✅ | - | 服务器地址列表 |
| ip | text | ❌ | - | 自定义虚拟IP |
| ctrl_port | int | ❌ | 11233 | 控制服务端口 |
| tunnel_port | int | ❌ | 0 | P2P通信端口（0=自动） |
| mtu | int | ❌ | 1400 | MTU设置 |

### Performance Optimization（性能优化）

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| rtx | bool | ❌ | False | QUIC优化传输 |
| fec | bool | ❌ | False | FEC前向纠错 |
| compress | bool | ❌ | False | LZ4压缩 |
| no_punch | bool | ❌ | False | 禁用P2P打洞 |

### Network Forwarding（网络转发）

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| input | text_list | ❌ | - | 入栈监听网段 |
| output | text_list | ❌ | - | 出栈允许网段 |
| no_nat | bool | ❌ | False | 禁用内置NAT |

### Port Mapping（端口映射）

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| port_mapping | text_list | ❌ | - | 端口映射规则 |
| allow_mapping | bool | ❌ | False | 允许作为映射出口 |

### Device Configuration（设备配置）

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| device_name | text | ❌ | - | 设备名称 |
| device_id | text | ❌ | - | 设备ID |
| tun_name | text | ❌ | vnt-tun | 虚拟网卡名称 |
| no_tun | bool | ❌ | False | 禁用TUN |

### Security Configuration（安全配置）

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| password | password | ❌ | - | 加密密码 |
| cert_mode | choice | ❌ | skip | 证书验证模式 |

### STUN Configuration（STUN配置）

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| udp_stun | text_list | ❌ | - | UDP打洞STUN服务器 |
| tcp_stun | text_list | ❌ | - | TCP打洞STUN服务器 |

## UI特性

### 1. 必填字段标识
- 必填字段的标签显示为**红色**
- 保存时验证必填字段是否为空

### 2. 工具提示
- 每个输入控件都有详细的工具提示
- 鼠标悬停时显示帮助信息

### 3. 占位符文本
- 文本框支持占位符提示
- 例如：`quic://1.2.3.4:29872`

### 4. 滚动支持
- 使用 wx.ScrolledWindow 支持滚动
- 适配不同屏幕尺寸

### 5. 响应式布局
- 窗口可调整大小
- 最小尺寸限制防止过度缩小
- DPI 感知缩放

### 6. 智能默认值
- 未配置的字段使用预定义默认值
- 减少用户输入负担

## 数据验证

### 保存时验证

```python
def collect_data_from_ui(self):
    data = {}
    
    for field_name, ctrl in self.input_controls.items():
        value = self.get_value_from_ctrl(field_name, ctrl)
        
        if value is not None:
            data[field_name] = value
        else:
            # 检查是否是必填字段
            config = self.CONFIG_FIELDS.get(field_name, {})
            if config.get('required', False):
                # 显示错误对话框
                return None
    
    return data
```

### 验证规则

1. **必填字段**: `network_code` 和 `server` 不能为空
2. **整数范围**: 端口号 0-65535，MTU 合理范围
3. **列表格式**: 每行一个条目或逗号分隔
4. **IP格式**: 可选的正则表达式验证（可扩展）

## 与旧版本的对比

### 旧版本（Tree视图）

**优点**:
- 可以查看原始数据结构
- 适合高级用户直接编辑

**缺点**:
- ❌ 不支持所有配置项
- ❌ 需要理解YAML结构
- ❌ 容易出错（缩进、类型等）
- ❌ 不友好的人机交互

### 新版本（分组表单）

**优点**:
- ✅ 支持**全部** VNT 2.0 配置项
- ✅ 直观的表单式界面
- ✅ 自动类型转换和验证
- ✅ 友好的工具提示和帮助
- ✅ 必填字段明确标识
- ✅ 智能默认值

**缺点**:
- 无法编辑未在 CONFIG_FIELDS 中定义的自定义字段
- 不适合直接操作复杂的嵌套结构

## 工作流程示例

### 场景1: 新建配置

```
1. 用户打开配置编辑器
2. 看到空的表单（带默认值）
3. 填写必填字段：
   - Network Code: "my_network"
   - Server Address: "quic://1.2.3.4:29872"
4. 可选填写其他字段
5. 点击 "Save and Exit"
6. 保存为 YAML 文件
7. vnt_daemon 自动转换为 TOML
8. vnt2_cli.exe 读取 TOML 并连接
```

### 场景2: 修改现有配置

```
1. 用户打开配置编辑器
2. 现有配置自动加载到表单
3. 修改需要的字段
4. 点击 "Save and Exit"
5. 覆盖原 YAML 文件
6. vnt_daemon 检测变更并重新转换
7. 重启 vnt2_cli.exe 应用新配置
```

### 场景3: 另存为新配置

```
1. 用户修改配置
2. 点击 "Save As..."
3. 选择新的文件名
4. 保存为新的 YAML 文件
5. 可在 Profile Manager 中切换配置
```

## 技术实现细节

### 1. 动态控件创建

```python
def add_config_field(self, sizer, parent, field_name, config):
    field_type = config['type']
    
    if field_type == 'text':
        ctrl = wx.TextCtrl(parent, ...)
    elif field_type == 'int':
        ctrl = wx.SpinCtrl(parent, ...)
    elif field_type == 'bool':
        ctrl = wx.CheckBox(parent, ...)
    # ...
    
    self.input_controls[field_name] = ctrl
```

### 2. 数据类型转换

```python
def get_value_from_ctrl(self, field_name, ctrl):
    if isinstance(ctrl, wx.TextCtrl):
        return ctrl.GetValue().strip()
    elif isinstance(ctrl, wx.SpinCtrl):
        return ctrl.GetValue()  # int
    elif isinstance(ctrl, wx.CheckBox):
        return ctrl.GetValue()  # bool
    elif isinstance(ctrl, wx.Choice):
        return ctrl.GetString(ctrl.GetSelection())  # str
```

### 3. 列表数据处理

```python
# YAML → UI（加载时）
if isinstance(value, list):
    ctrl.SetValue('\n'.join(str(v) for v in value))

# UI → YAML（保存时）
items = [item.strip() for item in text.split('\n') if item.strip()]
return items if items else None
```

## 相关文件

- 📄 [vnt_helper.py](vnt_helper.py) - VNT_YamlConfigEditor_Window 重新设计
- 📄 [vnt_daemon.py](vnt_daemon.py) - YAML to TOML 转换逻辑
- 📄 [VNT2_COMPLETE_SOLUTION.md](VNT2_COMPLETE_SOLUTION.md) - VNT 2.0 完整方案

---

**完成时间**: 2026-05-15  
**状态**: ✅ 已完成  
**影响范围**: 配置编辑器UI  
**风险等级**: 🟢 低（仅UI改进）  
**向后兼容**: ✅ 完全兼容（仍输出YAML格式）
