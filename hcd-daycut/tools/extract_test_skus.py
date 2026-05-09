import re
import sys
from pathlib import Path


SKU_RE = re.compile(r'["\']skuId["\']\s*:\s*["\']([^"\']+)["\']')
SKU_FUNC_RE = re.compile(r'sku\(\s*["\']([^"\']+)["\']')


def main() -> int:
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

