"""Excel 检查工具。用于输出工作簿结构和非空单元格，辅助确认测试数据格式。"""

import sys

import openpyxl


def _is_blank(v) -> bool:
    """根据输入状态和业务规则返回布尔判断结果。
    输出：bool

    Args:
        v (Any): 供当前处理流程使用的 `v` 值。

    Returns:
        bool: 条件满足、处理成功或校验通过时为 ``True``，否则为 ``False``。
    """
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def main(argv: list[str]) -> int:
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：argv（list[str]）
    输出：int

    Args:
        argv (list[str]): 供当前处理流程使用的 `argv` 值。

    Returns:
        int: 当前计算得到的数量、索引或编号。
    """
    path = argv[1] if len(argv) > 1 else "库管系统算法升级接口测试记录_20260419.xlsx"
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    print("sheets:", wb.sheetnames)

    for sname in wb.sheetnames:
        ws = wb[sname]
        print(f"\n== {sname} == rows={ws.max_row} cols={ws.max_column}")
        max_row = min(ws.max_row or 0, 60)
        max_col = min(ws.max_column or 0, 40)
        for r in range(1, max_row + 1):
            row = [ws.cell(r, c).value for c in range(1, max_col + 1)]
            if all(_is_blank(v) for v in row):
                continue
            print(r, row)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

