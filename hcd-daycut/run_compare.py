"""
运行仿真并自动将终端输出保存到日志文件。

使用方式：
    python scripts/run_with_log.py

默认执行 `python run.py`，并将输出写入 logs/run_YYYYMMDD_HHMMSS.txt。
如需自定义命令，可在命令行追加参数，例如：
    python scripts/run_with_log.py python run.py --cutoff-hour 6
"""

import subprocess
import sys
import datetime
from pathlib import Path
import re


def extract_params_from_run_py():
    """从run.py文件中提取参数设置"""
    try:
        run_py_path = Path("run.py")
        if not run_py_path.exists():
            return None
            
        content = run_py_path.read_text(encoding="utf-8")
        
        # 查找main函数中的参数设置
        inbound_allocation_strategy_match = re.search(r"inbound_allocation_strategy=['\"](\w+)['\"]", content)
        inbound_position_strategy_match = re.search(r"inbound_position_strategy=['\"](\w+)['\"]", content)
        scheduler_type_match = re.search(r"scheduler_type=['\"](\w+)['\"]", content)
        
        inbound_allocation_strategy = inbound_allocation_strategy_match.group(1) if inbound_allocation_strategy_match else "unknown"
        inbound_position_strategy = inbound_position_strategy_match.group(1) if inbound_position_strategy_match else "unknown"
        scheduler_type = scheduler_type_match.group(1) if scheduler_type_match else "unknown"
        
        # 创建缩写形式
        abbreviations = {
            "baseline": "base",
            "proposed": "prop",
            "heuristic": "heu",
            "optimization": "opt"
        }
        
        allocation_abbr = abbreviations.get(inbound_allocation_strategy, inbound_allocation_strategy)
        position_abbr = abbreviations.get(inbound_position_strategy, inbound_position_strategy)
        scheduler_abbr = abbreviations.get(scheduler_type, scheduler_type)
        
        # 使用allocation、position和scheduler三个参数
        return f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}"
    except Exception:
        return None


def modify_run_py(allocation_strategy, position_strategy, scheduler_type):
    """修改run.py文件中的策略参数"""
    try:
        run_py_path = Path("run.py")
        if not run_py_path.exists():
            return False
            
        content = run_py_path.read_text(encoding="utf-8")
        
        # 替换 argparse 默认值（run.py 通过命令行默认值运行）
        content = re.sub(
            r"(parser\.add_argument\('--inbound-allocation-strategy'[^\\n]*default=)['\"][^'\"]*['\"]",
            r"\1'" + allocation_strategy + "'",
            content
        )
        content = re.sub(
            r"(parser\.add_argument\('--inbound-position-strategy'[^\\n]*default=)['\"][^'\"]*['\"]",
            r"\1'" + position_strategy + "'",
            content
        )
        content = re.sub(
            r"(parser\.add_argument\('--scheduler-type'[^\\n]*default=)['\"][^'\"]*['\"]",
            r"\1'" + scheduler_type + "'",
            content
        )
        
        # 写回文件
        run_py_path.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[ERROR] 修改run.py失败: {e}")
        return False

# 在这里选择要对比的策略组合
def main():
    # 定义四组配置参数
    configs = [
        ("proposed", "proposed", "heuristic"),
    #    ("proposed", "proposed", "optimization"),
        ("baseline", "baseline", "heuristic"),
    #    ("baseline", "baseline", "optimization")
    ]
    
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    # 循环运行四组配置
    for i, (allocation, position, scheduler) in enumerate(configs, 1):
        print(f"[INFO] 开始运行第 {i}/4 组配置: {allocation}-{position}-{scheduler}")
        
        # 生成日志文件名
        abbreviations = {
            "baseline": "base",
            "proposed": "prop",
            "heuristic": "heu",
            "optimization": "opt"
        }
        
        allocation_abbr = abbreviations.get(allocation, allocation)
        position_abbr = abbreviations.get(position, position)
        scheduler_abbr = abbreviations.get(scheduler, scheduler)
        
        log_filename = f"{allocation_abbr}-{position_abbr}-{scheduler_abbr}.txt"
        log_file = logs_dir / log_filename
        
        print(f"[INFO] 日志将保存到: {log_file}")
        
        # 执行命令
        cmd = [
            "python", "run.py",
            "--inbound-allocation-strategy", allocation,
            "--inbound-position-strategy", position,
            "--scheduler-type", scheduler,
        ]
        print(f"[INFO] 执行命令: {' '.join(cmd)}")
        
        try:
            with log_file.open("w", encoding="utf-8") as f:
                # 同时将输出写到文件与当前终端
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # 合并标准错误到标准输出
                    text=True,
                    encoding="utf-8",
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    sys.stdout.write(line)
                    f.write(line)
                proc.wait()
                
            print(f"[INFO] 第 {i} 组配置运行完成，日志已保存到: {log_file}")
        except Exception as e:
            print(f"[ERROR] 运行第 {i} 组配置时出错: {e}")
    
    print("[INFO] 所有配置运行完成")


if __name__ == "__main__":
    main()
