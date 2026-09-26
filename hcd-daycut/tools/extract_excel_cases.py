"""Excel 测试用例提取工具。负责从测试工作簿中识别表头并导出可用于接口测试的记录。"""

import json
import sys
from dataclasses import dataclass
from typing import Any

import openpyxl


def _norm_header(v: Any) -> str:
    """执行 `_norm_header` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：v（Any）
    输出：str

    Args:
        v (Any): 供当前处理流程使用的 `v` 值。

    Returns:
        str: 当前处理得到的文本标识、格式化结果或诊断信息。
    """
    if v is None:
        return ""
    return str(v).strip()


def _is_blank(v: Any) -> bool:
    """根据输入状态和业务规则返回布尔判断结果。

    输入：v（Any）
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


@dataclass(frozen=True)
class Case:
    sheet: str
    row: int
    seq: str
    api: str
    ok_ng: str
    close_status: str
    target: str
    problem: str
    request: str
    expected_response: str


def _get(ws, r: int, c: int) -> Any:
    """读取并返回指定条件下的状态、对象或计算结果，不主动改变业务状态。
    输出：Any

    Args:
        ws (Any): 供当前处理流程使用的 `ws` 值。
        r (int): 供当前处理流程使用的 `r` 值。
        c (int): 供当前处理流程使用的 `c` 值。

    Returns:
        Any: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    return ws.cell(r, c).value


def _find_col(headers: dict[str, int], *names: str) -> int:
    """在候选集合中按既定约束查找匹配对象或可用位置。

    输入：headers（dict[str, int]）、*names（可变位置参数）
    输出：int

    Args:
        headers (dict[str, int]): 供当前处理流程使用的 `headers` 值。
        *names (tuple): 可变位置参数。

    Returns:
        int: 当前计算得到的数量、索引或编号。
    """
    for n in names:
        if n in headers:
            return headers[n]
    return 0


def extract(path: str) -> list[Case]:
    """执行 `extract` 对应的模块处理步骤，并返回该步骤产生的结果。

    输入：path（str）
    输出：list[Case]

    Args:
        path (str): 需要读取、写入或检查的文件路径。

    Returns:
        list[Case]: 当前处理流程产生的结果；具体结构由函数摘要说明。
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    cases: list[Case] = []

    for sname in wb.sheetnames:
        ws = wb[sname]
        if ws.max_row is None or ws.max_row < 2:
            continue

    # 第一行作为表头。
        headers: dict[str, int] = {}
        for c in range(1, (ws.max_column or 1) + 1):
            hv = _norm_header(_get(ws, 1, c))
            if hv:
                headers[hv] = c

        col_seq = _find_col(headers, "序号")
        col_api = _find_col(headers, "接口")
        col_target = _find_col(headers, "测试目标")
        col_req = _find_col(headers, "请求报文")
        col_resp = _find_col(headers, "响应报文")
        col_ok = _find_col(headers, "OK/NG")
        col_close = _find_col(headers, "关闭状态")
        col_problem = _find_col(headers, "问题点", "说明")

        if not col_seq and not col_api:
            continue

        for r in range(2, (ws.max_row or 2) + 1):
            seq = _get(ws, r, col_seq) if col_seq else None
            api = _get(ws, r, col_api) if col_api else None
            if _is_blank(seq) and _is_blank(api):
    # 多个工作表末尾带有空行，需要剔除。
                continue

            case = Case(
                sheet=sname,
                row=r,
                seq="" if seq is None else str(seq).strip(),
                api="" if api is None else str(api).strip(),
                ok_ng="" if not col_ok or _get(ws, r, col_ok) is None else str(_get(ws, r, col_ok)).strip(),
                close_status=""
                if not col_close or _get(ws, r, col_close) is None
                else str(_get(ws, r, col_close)).strip(),
                target=""
                if not col_target or _get(ws, r, col_target) is None
                else str(_get(ws, r, col_target)).strip(),
                problem=""
                if not col_problem or _get(ws, r, col_problem) is None
                else str(_get(ws, r, col_problem)).strip(),
                request=""
                if not col_req or _get(ws, r, col_req) is None
                else str(_get(ws, r, col_req)).strip(),
                expected_response=""
                if not col_resp or _get(ws, r, col_resp) is None
                else str(_get(ws, r, col_resp)).strip(),
            )
            cases.append(case)

    return cases


def main(argv: list[str]) -> int:
    """作为脚本入口，解析运行参数并按既定顺序调用本文件的主要处理流程。

    输入：argv（list[str]）
    输出：int

    Args:
        argv (list[str]): 供当前处理流程使用的 `argv` 值。

    Returns:
        int: 当前计算得到的数量、索引或编号。
    """
    default_path = "库管系统算法升级接口测试记录_20260419.xlsx"
    path = default_path
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        path = arg
        break
    cases = extract(path)

    open_ng = [
        c
        for c in cases
        if c.ok_ng in ("❌", "NG", "ng")
        and c.close_status not in ("已关闭", "关闭", "Closed")
    ]
    all_ng = [c for c in cases if c.ok_ng in ("❌", "NG", "ng")]

    print("total_cases:", len(cases))
    print("open_ng_cases:", len(open_ng))
    for c in open_ng:
        print(f"- [{c.sheet}] seq={c.seq} api={c.api} row={c.row}")
        if c.target:
            print("  target:", c.target.replace("\n", " ")[:160])
        if c.problem:
            print("  problem:", c.problem.replace("\n", " ")[:200])

    if "--all-ng" in argv:
        print("\nALL_NG_BEGIN")
        for c in all_ng:
            print(f"- [{c.sheet}] seq={c.seq} api={c.api} ok_ng={c.ok_ng} close={c.close_status} row={c.row}")
            if c.problem:
                print("  problem:", c.problem.replace("\n", " ")[:240])
        print("ALL_NG_END")

    # 如命令行要求，同时向标准输出写出 JSON。
    if "--json" in argv:
        payload = [c.__dict__ for c in cases]
        print("\nJSON_BEGIN")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("JSON_END")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
