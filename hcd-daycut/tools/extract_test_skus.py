"""测试 SKU 提取工具。负责从输入资料中汇总测试所需的 SKU 标识。"""

import re
import sys
from pathlib import Path


SKU_RE = re.compile(r'["\']skuId["\']\s*:\s*["\']([^"\']+)["\']')
SKU_FUNC_RE = re.compile(r'sku\(\s*["\']([^"\']+)["\']')


def main() -> int:
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：无显式业务输入；依赖实例字段或模块配置。
    输出：int

    Args:
        None: 无显式业务参数；使用实例状态或模块配置。

    Returns:
        int: 当前计算得到的数量、索引或编号。
    """
    root = Path(".")
    skus: set[str] = set()
    for path in root.rglob("test_*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        skus.update(SKU_RE.findall(text))
        skus.update(SKU_FUNC_RE.findall(text))
    for sku in sorted(skus):
        print(sku)
    print("\nTOTAL", len(skus))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

