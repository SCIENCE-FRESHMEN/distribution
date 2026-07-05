import re
import os
import sys
import matplotlib
import matplotlib.pyplot as plt

if os.name == 'nt':
    matplotlib.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei'] + matplotlib.rcParams.get('font.sans-serif', [])
else:
    matplotlib.rcParams['font.sans-serif'] = ['SimHei'] + matplotlib.rcParams.get('font.sans-serif', [])
matplotlib.rcParams['axes.unicode_minus'] = False

# 默认日志文件列表
DEFAULT_LOGS = ['base-base-heu.txt', 'log.txt']  # 默认日志
LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs')
DAILY_LOG_PATH = os.path.join(LOG_PATH, 'daily')
OUT_DIR = os.path.join(os.path.dirname(__file__), 'gantt_charts')
DATE_RE = re.compile(r"\b(20\d{6})\b")
# Match run_daily suffix: "-lam{POISSON_X}".
POISSON_X = 50  # [50,70,100,120]
SELECT_DATES = []
START_DATE = ""
END_DATE = ""

# 修改正则表达式以支持移库操作
TASK_RE = re.compile(r"第\s*\d+个(出库任务|入库任务|移库操作)\s+([A-Z0-9_\-]+|IN_\d+|OUTBOUND[\w_\-]+).*?\(巷道\s*(\d+)\)，起止\s*([0-9.]+)s~([0-9.]+)s", re.UNICODE)


def parse_days(log_text, days=None):
    results = {}
    
    #  "=== 第n天 ===" 段
    reloc_day_positions = {}
    for m in re.finditer(r"=== 第(\d+)天 ===", log_text):
        day = int(m.group(1))
        reloc_day_positions[day] = m.start()
    
    #  "第 n 天汇总" 段
    stock_day_positions = {}
    summary_pos = log_text.find('全部天汇总')
    if summary_pos != -1:
        for m in re.finditer(r"第\s*(\d+)\s*天汇总", log_text[summary_pos:]):
            day = int(m.group(1))
            stock_day_positions[day] = summary_pos + m.start()

    if days is None:
        days = sorted(set(reloc_day_positions) | set(stock_day_positions))

    if not days:
        return results

    for d in days:
        tasks = []
        stock_tasks = []  
        reloc_tasks = []  
        
        # 移库
        if d in reloc_day_positions:
            start_pos = reloc_day_positions[d]
            end_pos = None
            for nd in range(d+1, max(days)+2):
                if nd in reloc_day_positions:
                    end_pos = reloc_day_positions[nd]
                    break
            if end_pos is None:
                end_pos = len(log_text)
            
            segment = log_text[start_pos:end_pos]
            for m in TASK_RE.finditer(segment):
                task_type = m.group(1)
                if '移库' in task_type:  # 只处理移库操作
                    ttype = '移库'
                    tid = m.group(2)
                    aisle = int(m.group(3))
                    start = float(m.group(4))
                    end = float(m.group(5))
                    reloc_tasks.append({'type': ttype, 'id': tid, 'aisle': aisle, 'start': start, 'end': end})
        
        # 入库出库
        if d in stock_day_positions:
            start_pos = stock_day_positions[d]
            end_pos = None
            for nd in range(d+1, max(days)+2):
                if nd in stock_day_positions:
                    end_pos = stock_day_positions[nd]
                    break
            if end_pos is None:
                end_pos = len(log_text)
            
            segment = log_text[start_pos:end_pos]
            for m in TASK_RE.finditer(segment):
                task_type = m.group(1)
                if '出库' in task_type:
                    ttype = '出库'
                    tid = m.group(2)
                    aisle = int(m.group(3))
                    start = float(m.group(4))
                    end = float(m.group(5))
                    stock_tasks.append({'type': ttype, 'id': tid, 'aisle': aisle, 'start': start, 'end': end})
                    tasks.append({'type': ttype, 'id': tid, 'aisle': aisle, 'start': start, 'end': end})
                elif '入库' in task_type:
                    ttype = '入库'
                    tid = m.group(2)
                    aisle = int(m.group(3))
                    start = float(m.group(4))
                    end = float(m.group(5))
                    tasks.append({'type': ttype, 'id': tid, 'aisle': aisle, 'start': start, 'end': end})
                else:
                    continue  
        
        # 校验移库任务
        corrected_reloc_tasks = []
        for reloc_task in reloc_tasks:
            task_id = reloc_task['id']
            reloc_aisle = reloc_task['aisle']
            
            # 查找同一任务ID的出库任务
            matching_stock_tasks = [t for t in stock_tasks if t['id'] == task_id]
            
            if matching_stock_tasks:
                # 找到对应的出库任务，以出库任务的巷道为准
                stock_task = matching_stock_tasks[0]
                stock_aisle = stock_task['aisle']
                
                if reloc_aisle != stock_aisle:
                    print(f"第{d}天警告: 任务 {task_id} 的巷道不一致 - 移库记录为巷道{reloc_aisle}，出库记录为巷道{stock_aisle}，已修正为巷道{stock_aisle}")
                    # 修正巷道为出库任务的巷道，但保留其他信息
                    corrected_task = reloc_task.copy()
                    corrected_task['aisle'] = stock_aisle
                    corrected_reloc_tasks.append(corrected_task)
                else:
                    corrected_reloc_tasks.append(reloc_task)
            else:
                corrected_reloc_tasks.append(reloc_task)
        
        # 添加修正后的移库任务
        tasks.extend(corrected_reloc_tasks)
        results[d] = tasks
    
    #如果出库和移库是同一ID，改为移库
    for d in days:
        task_dict = {}
        for task in results[d]:
            task_id = task['id']
            if task_id in task_dict:
                # 如果已经存在相同ID的任务，检查类型优先级：移库 > 出库 > 入库
                existing_task = task_dict[task_id]
                priority = {'移库': 2, '出库': 1, '入库': 0}
                
                if priority.get(task['type'], -1) > priority.get(existing_task['type'], -1):
                    # 新任务优先级更高，替换
                    task_dict[task_id] = task
                elif priority.get(task['type'], -1) == priority.get(existing_task['type'], -1):
                    # 优先级相同，选择开始时间更早的
                    if task['start'] < existing_task['start']:
                        task_dict[task_id] = task
            else:
                task_dict[task_id] = task
        
        results[d] = list(task_dict.values())
    
    return results


def plot_day(tasks, day, out_dir=OUT_DIR, show_labels=False, title_label=None, file_tag=None):
    # tasks: list of dicts with aisle, start, end, type
    aisles = [1,2,3,4,5]
    y_pos = {a: i for i,a in enumerate(aisles)}
    fig, ax = plt.subplots(figsize=(10, 4)) 
    colors = {'出库':'#1f77b4', '入库':'#ff7f0e', '移库':'#2ca02c'}
    title_label = title_label or f"第{day}天"
    file_tag = file_tag or f"day{day}"

    # 如果没有任务，直接绘制空图
    if not tasks:
        ax.set_yticks(list(y_pos.values()))
        ax.set_yticklabels([f"巷道 {a}" for a in aisles])
        ax.set_xlabel('秒（相对于当日起点）')
        ax.set_title(f'{title_label} — 五个巷道 任务甘特图')
        ax.grid(axis='x', linestyle='--', alpha=0.3)
        out_path = os.path.join(out_dir, f'gantt_{file_tag}.png')
        plt.tight_layout()
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    # 去重：按任务 ID 保留首次出现（最早 start）
    tasks_sorted = sorted(tasks, key=lambda x: x['start'])
    unique = {}
    for t in tasks_sorted:
        if t['id'] not in unique:
            unique[t['id']] = t
    tasks = list(unique.values())

    # 将时间轴相对于当天起点显示，避免把前一天的绝对时间显示到当前图中
    day_base = min(t['start'] for t in tasks)

    # 检测每个巷道内的重叠：对每个巷道按开始时间排序
    overlap_info = []  # 存储重叠任务信息
    
    for t in tasks:
        t['overlap'] = False
    
    # 按巷道分组检测重叠
    for a in aisles:
        # 获取当前巷道的所有任务
        aisle_tasks = [t for t in tasks if t['aisle'] == a]
        if not aisle_tasks:
            continue
            
        # 按开始时间排序
        aisle_tasks.sort(key=lambda x: x['start'])
        
        # 检测同一巷道内的任务重叠
        for i in range(len(aisle_tasks)):
            for j in range(i+1, len(aisle_tasks)):
                task_i = aisle_tasks[i]
                task_j = aisle_tasks[j]
                
                # 确保是同一巷道
                if task_i['aisle'] != task_j['aisle']:
                    continue
                    
                # 如果任务i结束时间 > 任务j开始时间，则存在重叠
                if task_i['end'] > task_j['start']:
                    # 计算重叠时间
                    overlap_start = max(task_i['start'], task_j['start'])
                    overlap_end = min(task_i['end'], task_j['end'])
                    overlap_duration = overlap_end - overlap_start
                    
                    if overlap_duration > 0:
                        # 检查重叠中是否包含移库操作
                        has_reloc = (task_i['type'] == '移库' or task_j['type'] == '移库')
                        
                        overlap_info.append({
                            'aisle': a,
                            'task1_id': task_i['id'],
                            'task1_type': task_i['type'],
                            'task2_id': task_j['id'],
                            'task2_type': task_j['type'],
                            'overlap_start': overlap_start - day_base,
                            'overlap_end': overlap_end - day_base,
                            'duration': overlap_duration,
                            'has_reloc': has_reloc
                        })
                        
                        if not has_reloc:
                            task_i['overlap'] = True
                            task_j['overlap'] = True

    for t in tasks:
        if t['aisle'] not in y_pos:
            continue
        # 只绘制在当天记录范围内开始的任务（避免把前一天开始的任务显示到后一天）
        if t['start'] < day_base:
            continue
        y = y_pos[t['aisle']]
        left = t['start'] - day_base
        width = t['end'] - t['start']
        
        # 确定条形颜色：如果标记为重叠则用红色，否则按任务类型配色
        if t.get('overlap'):
            bar_color = '#d62728'  # 红色表示需要关注的冲突
        else:
            bar_color = colors.get(t['type'], 'gray')
            
        ax.barh(y, width, left=left, height=0.6, color=bar_color)
        if show_labels:
            ax.text(left + 0.5, y, t['id'], va='center', ha='left', fontsize=6)

    ax.set_yticks(list(y_pos.values()))
    ax.set_yticklabels([f"巷道 {a}" for a in aisles])
    ax.set_xlabel('秒（相对于当日起点）')
    ax.set_title(f'{title_label} — 五个巷道 任务甘特图')
    ax.grid(axis='x', linestyle='--', alpha=0.3)

    # 添加图注（图例）
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor=colors['出库'], label='出库任务'),
        Patch(facecolor=colors['入库'], label='入库任务'),
        # Patch(facecolor=colors['移库'], label='移库操作'),
        Patch(facecolor='#d62728', label='巷道内冲突')
    ]
    ax.legend(handles=legend_elements, loc='upper right', bbox_to_anchor=(1.0, 1.0))

    out_path = os.path.join(out_dir, f'gantt_{file_tag}.png')
    plt.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def _format_lambda_suffix(value):
    if value is None:
        return ""
    text = f"{value}".replace(".", "p")
    return f"-lam{text}"


def _filter_logs_by_lambda(files, lambda_value):
    if lambda_value is None:
        return files
    suffix = _format_lambda_suffix(lambda_value)
    return [f for f in files if suffix in os.path.basename(f)]

def _has_date_filter():
    return bool(SELECT_DATES or START_DATE or END_DATE)


def _extract_date_dir(dir_name):
    match = DATE_RE.search(dir_name)
    return match.group(1) if match else None


def _filter_date_dirs(date_dirs):
    if not _has_date_filter():
        return date_dirs
    if SELECT_DATES:
        allowed = set(SELECT_DATES)
        return [d for d in date_dirs if _extract_date_dir(d) in allowed]
    filtered = []
    for d in date_dirs:
        date_key = _extract_date_dir(d)
        if not date_key:
            continue
        if START_DATE and date_key < START_DATE:
            continue
        if END_DATE and date_key > END_DATE:
            continue
        filtered.append(d)
    return filtered


def _collect_log_files(items):
    logs = []
    for item in items:
        if os.path.isdir(item):
            files = [
                os.path.join(item, f) for f in os.listdir(item) if f.endswith('.txt')
            ]
            # Only filter by lambda for daily date folders.
            if os.path.normpath(item).startswith(os.path.normpath(DAILY_LOG_PATH)):
                files = _filter_logs_by_lambda(files, POISSON_X)
            logs.extend(files)
            continue
        if DATE_RE.fullmatch(item):
            daily_dir = os.path.join(DAILY_LOG_PATH, item)
            if os.path.isdir(daily_dir):
                files = [
                    os.path.join(daily_dir, f) for f in os.listdir(daily_dir) if f.endswith('.txt')
                ]
                files = _filter_logs_by_lambda(files, POISSON_X)
                logs.extend(files)
                continue
        logs.append(item)
    return logs


def _extract_date_from_path(log_path):
    parts = os.path.normpath(log_path).split(os.sep)
    for part in parts:
        if DATE_RE.fullmatch(part):
            return part
    base = os.path.basename(log_path)
    m = DATE_RE.search(base)
    return m.group(1) if m else None


def _extract_date_from_text(log_text):
    m = re.search(r"运行日期\s*(\d{8})", log_text)
    if m:
        return m.group(1)
    m = re.search(r"日期\s*(\d{8})", log_text)
    return m.group(1) if m else None


def find_log_file():
    """查找日志文件，按以下顺序尝试"""
    # 1. 检查命令行参数
    if len(sys.argv) > 1:
        return _collect_log_files(sys.argv[1:])

    if _has_date_filter():
        if os.path.exists(DAILY_LOG_PATH):
            day_dirs = [
                d for d in os.listdir(DAILY_LOG_PATH)
                if os.path.isdir(os.path.join(DAILY_LOG_PATH, d))
            ]
            day_dirs = _filter_date_dirs(day_dirs)
            daily_files = []
            for day_folder in day_dirs:
                day_dir = os.path.join(DAILY_LOG_PATH, day_folder)
                for fname in os.listdir(day_dir):
                    if fname.endswith('.txt'):
                        daily_files.append(os.path.join(day_dir, fname))
            daily_files = _filter_logs_by_lambda(daily_files, POISSON_X)
            if daily_files:
                return daily_files
        return None
    
    # 2. 检查当前目录下的默认日志文件
    for default_log in DEFAULT_LOGS:
        full_path = os.path.join(os.getcwd(), default_log)
        if os.path.exists(full_path):
            return [full_path]
    
    # 3. 检查logs目录下的默认日志文件
    for default_log in DEFAULT_LOGS:
        full_path = os.path.join(LOG_PATH, default_log)
        if os.path.exists(full_path):
            return [full_path]
    
    # 4. 如果都没找到，返回None，让main函数处理
    return None


def main(log_files=None):
    if log_files is None:
        if _has_date_filter():
            print("日期筛选已启用，使用日仿真日志结果。")
        else:
            print("未启用日期筛选，使用 DEFAULT_LOGS 结果。")
        log_files = find_log_file()
        if log_files is None:
            print("未找到默认日志文件，请提供要处理的日志文件路径:")
            print("例如: python plot_gantt_from_log.py logs/logfile.txt")
            print("或者将日志文件放在脚本同目录下，命名为 base-base-heu.txt")
            return

    for log in log_files:
        # 如果提供了相对路径而不是绝对路径，尝试在常见位置查找日志文件
        if not os.path.isabs(log):
            lp = os.path.join(os.getcwd(), log)  # 当前工作目录
            if not os.path.exists(lp):
                lp = os.path.join(os.path.dirname(__file__), log)  # 脚本所在目录
            if not os.path.exists(lp):
                lp = os.path.join(LOG_PATH, log)  # logs目录下
        else:
            lp = log

        if not os.path.exists(lp):
            print(f"错误: 日志文件不存在: {lp}")
            continue

        print(f"正在解析日志文件: {lp}")
        with open(lp, 'r', encoding='utf-8') as f:
            text = f.read()

        date_str = _extract_date_from_path(lp) or _extract_date_from_text(text)
        results = parse_days(text)
        if not results:
            print(f"未解析到天数信息，跳过: {lp}")
            continue

        base_name = os.path.splitext(os.path.basename(log))[0]
        if date_str:
            out_dir = os.path.join(os.path.dirname(__file__), 'gantt_charts', 'daily', date_str, base_name)
        else:
            out_dir = os.path.join(os.path.dirname(__file__), 'gantt_charts', base_name)
        os.makedirs(out_dir, exist_ok=True)
        out_files = []
        print("开始生成甘特图...")
        for d, tasks in results.items():
            day_label = f"{date_str} 第{d}天" if date_str else f"第{d}天"
            file_tag = f"{date_str}_day{d}" if date_str else f"day{d}"
            print(f"正在处理{day_label}的任务...共 {len(tasks)} 个任务")
            out = plot_day(tasks, d, out_dir=out_dir, title_label=day_label, file_tag=file_tag)
            out_files.append(out)
        print('\n生成完成:')
        for p in out_files:
            print(p)


if __name__ == '__main__':
    main()
