"""生成 Web Push VAPID 密钥对，用于 `integrations.push` 配置。

用法：cd server && ../.venv/bin/python scripts/generate_vapid_keys.py
输出格式即 Web Push 生态标准格式：
- 私钥 = P-256 d 参数的 base64url（43 字符），填 vapid_private_key_secret_value
  或写入 ARIA_VAPID_PRIVATE_KEY 环境变量（vapid_private_key_secret_ref 引用）；
- 公钥 = 未压缩点 04||X||Y 的 base64url（87 字符），填 vapid_public_key。
注意：py_vapid 的 from_string 只接受 d 值格式，不接受 PEM。
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid  # type: ignore[import-untyped]


def main() -> None:
    vapid = Vapid()
    vapid.generate_keys()
    private_value = vapid.private_key.private_numbers().private_value
    private_key = base64.urlsafe_b64encode(
        private_value.to_bytes(32, "big")
    ).rstrip(b"=").decode()
    public_key = base64.urlsafe_b64encode(
        vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    ).rstrip(b"=").decode()
    print(f"vapid_public_key: {public_key}")
    print(f"vapid_private_key_secret_value: {private_key}")
    print("（私钥也可放入 ARIA_VAPID_PRIVATE_KEY 并用 secret_ref: env:ARIA_VAPID_PRIVATE_KEY）")


if __name__ == "__main__":
    main()
