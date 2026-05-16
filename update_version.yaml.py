# import os
import re
import hashlib
import yaml

# 定义路径
VNT_HELPER_PY_PATH = r".\vnt_helper.py"
VNT_HELPER_ZIP_PATH = r".\dist\vnt_helper.zip"
VERSION_YAML_PATH = r".\dist\version.yaml"


def extract_version_from_py(file_path):
    """从 vnt_helper.py 中提取 VNT_HELPER_VERSION 的值"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    match = re.search(r'VNT_HELPER_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        raise ValueError("未找到 VNT_HELPER_VERSION 定义")
    return match.group(1)


def calculate_sha256(file_path):
    """计算文件的 SHA256 哈希值"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def update_version_yaml(version, checksum, yaml_path):
    """更新 version.yaml 文件中的 version 和 checksum 字段"""
    # 读取现有 YAML 内容
    with open(yaml_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    # 更新字段
    data['version'] = version
    data['checksum'] = checksum

    # 写回文件（保留原有注释以外的内容格式）
    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def main():
    print("正在提取版本号...")
    version = extract_version_from_py(VNT_HELPER_PY_PATH)
    print(f"提取到的版本号: {version}")

    print("正在计算 ZIP 文件的 SHA256...")
    checksum = calculate_sha256(VNT_HELPER_ZIP_PATH)
    print(f"SHA256 校验和: {checksum}")

    print("正在更新 version.yaml...")
    update_version_yaml(version, checksum, VERSION_YAML_PATH)
    print("version.yaml 已成功更新！")


if __name__ == "__main__":
    main()
