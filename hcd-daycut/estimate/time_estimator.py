from typing import List, Optional, Dict, Any, Tuple, Union
from pathlib import Path
import json
import numpy as np
import pandas as pd
import joblib
from datetime import time, timedelta
from math import sqrt

from config_loader import load_jsonc
from simulation.position import InventoryPosition

import warnings
from sklearn.base import InconsistentVersionWarning

warnings.filterwarnings('ignore', category=InconsistentVersionWarning)


def load_time_estimator_config(path: Optional[str]) -> dict:
    """
    Load estimator config from a JSON file if provided.
    """
    if not path:
        return {}
    cfg_path = Path(path)
    if not cfg_path.exists():
        return {}
    try:
        return load_jsonc(cfg_path)
    except Exception:
        return {}


class TimeEstimator:
    """
    TimeEstimator
    - 使用物理建模（sequence 构建 + 2D 物理时间估计）
    - 使用外部提供的随机森林模型预测“残差”（residual），并将残差叠加到物理时间上得到最终预测
    """

    def __init__(self,
                 model_path: str = "estimate/model/estimator_model_residual_models.pkl",
                 pickup_time_default: float = 17.0,
                 drop_time_default: float = 17.0,
                 dock_in_col: int = 1,
                 dock_out_col: int = 3,
                 dock_map_in: Optional[Dict[int, int]] = None,
                 dock_map_out: Optional[Dict[int, int]] = None,
                 load_model: bool = False,
                 col_scale: float = 15.0,
                 layer_scale: float = 0.5,
                 v_col_max: float = 1.5,
                 v_layer_max: float = 0.625,
                 a_col: float = 0.15,
                 a_layer: float = 0.075,
                 config_path: Optional[str] = "config/time_estimator.json",
                 ):
        """
        Args:
            model_path: path to residual model file
            pickup_time_default, drop_time_default: fallback pick/drop seconds when missing in data
            load_model: whether to load residual models
            config_path: config json path (default "config/time_estimator.json")
        """
        cfg = load_time_estimator_config(config_path)

        # load model path from config (if any)
        self.model_path = cfg.get("model_path", model_path)

        self.pickup_time_default = float(cfg.get("pickup_time_default", pickup_time_default))
        self.drop_time_default = float(cfg.get("drop_time_default", drop_time_default))
        self.dock_in_col = int(cfg.get("dock_in_col", dock_in_col))
        self.dock_out_col = int(cfg.get("dock_out_col", dock_out_col))

        cfg_dock_map_in = cfg.get("dock_map_in", None)
        cfg_dock_map_out = cfg.get("dock_map_out", None)
        self.dock_map_in = {int(k): int(v) for k, v in (cfg_dock_map_in or dock_map_in or {1: 1, 2: 5, 3: 9}).items()}
        self.dock_map_out = {int(k): int(v) for k, v in (cfg_dock_map_out or dock_map_out or {1: 1, 2: 11, 3: 16}).items()}

        self.load_model = bool(cfg.get("load_model", load_model))

        physics_cfg = cfg.get("physics", {})
        self.physics_params = {
            "col_scale": float(physics_cfg.get("col_scale", col_scale)),
            "layer_scale": float(physics_cfg.get("layer_scale", layer_scale)),
            "v_col_max": float(physics_cfg.get("v_col_max", v_col_max)),
            "v_layer_max": float(physics_cfg.get("v_layer_max", v_layer_max)),
            "a_col": float(physics_cfg.get("a_col", a_col)),
            "a_layer": float(physics_cfg.get("a_layer", a_layer)),
        }
        
        # load trained residual models if available
        self.inbound_model = None
        self.outbound_model = None
        if self.load_model:
            self._load_model()

        # notebook 中使用的残差特征顺序
        self.feature_names = [
            "Δ列1", "Δ层1", "Δ列2", "Δ层2", "Δ列3", "Δ层3", "GL_Row", "raw_layer"
        ]

    def _load_model(self):
        """加载时间预测模型"""
        try:
            models = joblib.load(self.model_path)
            self.inbound_model = models["inbound_model"]
            self.outbound_model = models["outbound_model"]
            # print(f"[INFO] 成功加载入库模型: {type(self.inbound_model).__name__}")
            # print(f"[INFO] 成功加载出库模型: {type(self.outbound_model).__name__}")
        except Exception as e:
            print(f"[ERROR] 模型加载失败: {e}")
            # 如果加载失败，保留None，后续逻辑会处理

    # -----------------------------
    # Public estimate APIs
    # -----------------------------
    def estimate_inbound_time(self,
                              target_position: List[InventoryPosition],
                              skus: List[Dict[str, Any]],
                              in_line: int = 1,
                              current_position: Optional[InventoryPosition] = None,
                              ) -> float:
        """
        入库单任务时间估计
        Args:
            target_position: List[InventoryPosition]
            skus: SKU列表
            in_line: 入库线编号
            current_position: 堆垛机当前位置
        Returns:
            total_time: 总时间（秒）
        """
        # return 70
        # 判断sku数量，确定是单梁入库还是双梁入库
        dual = len(skus) > 1

        # 根据单双梁决定创建多少条记录
        if dual:
            # 双梁入库需要两条记录，但要确保 target_position 有足够的元素
            if len(target_position) < 2:
                # 如果 target_position 不足两个元素，说明放在同一货位，当作单梁处理
                dual = False
                df_tmp = pd.DataFrame([{
                    "InstructionID": 9998,
                    "OUTorIN": 0,
                    "QROrder": 1,
                    "GL_Roadway": 1,
                    "GL_Row": int(target_position[0].row) if target_position else 0,
                    "GL_Column": int(target_position[0].column) if target_position else 0,
                    "GL_Layer": int(target_position[0].level) if target_position else 0,
                    "raw_layer": int(in_line) if in_line is not None else 0,
                    "StartTime": pd.Timestamp.now(),
                    "EndTime": pd.Timestamp.now(),
                    "UseTime": None
                }])
            else:
                # 双梁入库需要两条记录
                df_tmp = pd.DataFrame([
                    {
                        "InstructionID": 9998,
                        "OUTorIN": 0,
                        "QROrder": 1,
                        "GL_Roadway": 1,
                        "GL_Row": int(target_position[0].row),
                        "GL_Column": int(target_position[0].column),
                        "GL_Layer": int(target_position[0].level),
                        "raw_layer": int(in_line),
                        "StartTime": pd.Timestamp.now(),
                        "EndTime": pd.Timestamp.now(),
                        "UseTime": None
                    },
                    {
                        "InstructionID": 9999,
                        "OUTorIN": 0,
                        "QROrder": 2,
                        "GL_Roadway": 1,
                        "GL_Row": int(target_position[1].row),
                        "GL_Column": int(target_position[1].column),
                        "GL_Layer": int(target_position[1].level),
                        "raw_layer": int(in_line),
                        "StartTime": pd.Timestamp.now(),
                        "EndTime": pd.Timestamp.now(),
                        "UseTime": None
                    }])
        else:
            # 单梁入库只需要一条记录
            df_tmp = pd.DataFrame([{
                "InstructionID": 9998,
                "OUTorIN": 0,
                "QROrder": 1,
                "GL_Roadway": 1,
                "GL_Row": int(target_position[0].row),
                "GL_Column": int(target_position[0].column),
                "GL_Layer": int(target_position[0].level),
                "raw_layer": int(in_line),
                "StartTime": pd.Timestamp.now(),
                "EndTime": pd.Timestamp.now(),
                "UseTime": None
            }])

        seq = self._build_sequence(
            df_tmp,
            pickup_time=self.pickup_time_default,
            drop_time=self.drop_time_default,
            dock_in_col=self.dock_in_col,
            dock_out_col=self.dock_out_col,
            dock_map_in=self.dock_map_in,
            dock_map_out=self.dock_map_out,
            start_pos_dict=current_position,
            default_dock_layer=1
        )
        seq = self._augment_sequence_physics2d(seq, **self.physics_params)
        
        if dual:
            # 双梁入库只计算第二个任务的时间
            i = 1
        else:
            # 单梁入库计算第一个（也是唯一一个）任务的时间
            i = 0
            
        physics_time = float(seq.iloc[i].get("physics_time_total", 0.0))
        pickdrop = float(seq.iloc[i].get("取放时间_sec", self.pickup_time_default + self.drop_time_default))
        
        # residual features
        X = pd.DataFrame([seq.iloc[i][self.feature_names].values], columns=self.feature_names)
        
        residual = 0.0
        if self.inbound_model is not None:
            pred = getattr(self.inbound_model, 'predict')(X)
            residual = float(pred[0])
        else:
            # 若没有模型，则使用默认值
            residual = 3.0
        
        total_time = residual + physics_time + pickdrop

        return float(total_time)

    def estimate_outbound_time(self,
                               source_position,
                               skus: List[Dict[str, Any]],
                               production_line: int = 1,
                               current_position: Optional[InventoryPosition] = None,
                               ) -> float:
        """
        出库单任务时间估计
        Args:
            source_position: (GL_Row, GL_Column, GL_Layer)
            skus, raw_layer, current_position: same as inbound
        Returns:
            total_time,
        """
        # return 70
        df_tmp = pd.DataFrame([{
            "InstructionID": 9998,
            "OUTorIN": 1,  # 出库
            "QROrder": 1,
            "GL_Roadway": 1,
            "GL_Row": int(source_position[0].row),
            "GL_Column": int(source_position[0].column),
            "GL_Layer": int(source_position[0].level),
            "raw_layer": production_line,
            "StartTime": pd.Timestamp.now(),
            "EndTime": pd.Timestamp.now(),
            "UseTime": None
        }])

        seq = self._build_sequence(
            df_tmp,
            pickup_time=self.pickup_time_default,
            drop_time=self.drop_time_default,
            dock_in_col=self.dock_in_col,
            dock_out_col=self.dock_out_col,
            dock_map_in=self.dock_map_in,
            dock_map_out=self.dock_map_out,
            start_pos_dict=current_position,
            default_dock_layer=1
        )
        seq = self._augment_sequence_physics2d(seq, **self.physics_params)
        physics_time = float(seq.iloc[0].get("physics_time_total", 0.0))
        pickdrop = float(seq.iloc[0].get("取放时间_sec", self.pickup_time_default + self.drop_time_default))

        X = seq[self.feature_names]
        residual = 0.0
        if self.outbound_model is not None:
            pred = getattr(self.outbound_model, 'predict')(X)
            residual = float(pred[0])
        else:
            # 若没有模型，则使用默认值
            residual = 3.0

        total_time = residual + physics_time + pickdrop
        
        return float(total_time)

    def _build_sequence(
        self,
        df,
        pickup_time=17.0,
        drop_time=17.0,
        dock_in_col=None,
        dock_out_col=None,
        default_dock_layer=1,
        dock_map_in=None,
        dock_map_out=None,
        start_pos_dict=None,
    ):
        """
        构建任务序列特征
        每个 roadway 有通过start_pos_dict指定独立的起始位置
        增加字段：
            - 取放时间_sec：根据任务类型和QROrder自动计算
            - 移动时间_sec：= 总时间 - 取放时间_sec
        """
        # 如果没有提供dock_in_col和dock_out_col，则使用实例变量
        if dock_in_col is None:
            dock_in_col = self.dock_in_col
        if dock_out_col is None:
            dock_out_col = self.dock_out_col

        def _parse_task(x):
            if x in [0, 1, 3, 4]:
                return int(x)
            return np.nan

        def _to_seconds(val):
            if pd.isna(val):
                return np.nan
            if isinstance(val, timedelta):
                return val.total_seconds()
            if isinstance(val, time):
                return val.hour * 3600 + val.minute * 60 + val.second
            if isinstance(val, str):
                try:
                    h, m, s = map(int, val.split(":"))
                    return h * 3600 + m * 60 + s
                except Exception:
                    return np.nan
            return np.nan

        dock_map_in = dock_map_in or {}
        dock_map_out = dock_map_out or {}

        df2 = df.copy()
        df2 = df2.sort_values(["GL_Roadway", "StartTime"]).reset_index(drop=True)

        # === 初始化各roadway的start_pos ===
        start_pos_per_roadway = {}
        for roadway in df2["GL_Roadway"].unique():
            if start_pos_dict and not isinstance(start_pos_dict, dict) and hasattr(start_pos_dict, 'column') and hasattr(start_pos_dict, 'level'):
                # 如果是InventoryPosition对象，则使用其column和level作为起始位置
                start_pos_per_roadway[roadway] = (int(start_pos_dict.column), int(start_pos_dict.level))
            elif isinstance(start_pos_dict, dict) and roadway in start_pos_dict:
                start_pos_per_roadway[roadway] = start_pos_dict[roadway]
            else:
                dock_in_layer = dock_map_in.get(roadway, default_dock_layer)
                dock_out_layer = dock_map_out.get(roadway, default_dock_layer)
                if roadway in dock_map_in:
                    start_pos_per_roadway[roadway] = (dock_in_col, dock_in_layer)
                elif roadway in dock_map_out:
                    start_pos_per_roadway[roadway] = (dock_out_col, dock_out_layer)
                else:
                    start_pos_per_roadway[roadway] = (dock_in_col, default_dock_layer)

        recs = []
        for roadway, g in df2.groupby("GL_Roadway", sort=True):
            prev_end = None
            last_qrorder = None
            last_seg1 = (0, 0)
            last_seg2 = (0, 0)

            for _, row in g.iterrows():
                task = _parse_task(row["OUTorIN"])
                if pd.isna(task):
                    continue

                tgt_row = int(row["GL_Row"])
                tgt_col = int(row["GL_Column"])
                tgt_layer = int(row["GL_Layer"])
                qrorder = int(row["QROrder"]) if not pd.isna(row["QROrder"]) else None

                raw_layer = None
                if task in [0, 1]:
                    raw_layer = int(row.get("raw_layer", default_dock_layer))

                if task == 0:  # 入库
                    dock_col = dock_in_col
                    dock_layer = dock_map_in.get(raw_layer, raw_layer) if raw_layer is not None else default_dock_layer
                elif task == 1:  # 出库
                    dock_col = dock_out_col
                    dock_layer = dock_map_out.get(raw_layer, raw_layer) if raw_layer is not None else default_dock_layer
                else:
                    dock_col = dock_in_col
                    dock_layer = default_dock_layer

                if prev_end is None:
                    start_col, start_layer = start_pos_per_roadway[roadway]
                else:
                    start_col, start_layer = prev_end

                run_sec = _to_seconds(row["UseTime"])

                # === 统一计算取放时间 ===
                if task == 0:  # 入库
                    if qrorder == 1:
                        pick_drop_time = pickup_time + drop_time
                    elif qrorder == 2 and last_qrorder == 1:
                        pick_drop_time = pickup_time + 2 * drop_time  # 双梁入库
                    else:
                        pick_drop_time = pickup_time + drop_time
                else:
                    pick_drop_time = pickup_time + drop_time

                # === 路径差值计算 ===
                if task == 0 and qrorder == 1:  # 入库:start -> dock -> target
                    seg1_col = abs(start_col - dock_col)
                    seg1_layer = abs(start_layer - dock_layer)
                    seg2_col = abs(dock_col - tgt_col)
                    seg2_layer = abs(dock_layer - tgt_layer)
                    seg3_col, seg3_layer = 0, 0
                    end_pos = (tgt_col, tgt_layer)

                elif task == 0 and qrorder == 2 and last_qrorder == 1:
                    seg1_col, seg1_layer = last_seg1
                    seg2_col, seg2_layer = last_seg2
                    seg3_col = abs(start_col - tgt_col)
                    seg3_layer = abs(start_layer - tgt_layer)
                    end_pos = (tgt_col, tgt_layer)

                elif task == 0 and qrorder == 2 and last_qrorder != 1:
                    seg1_col = abs(start_col - tgt_col)
                    seg1_layer = abs(start_layer - tgt_layer)
                    seg2_col = abs(dock_col - tgt_col)
                    seg2_layer = abs(dock_layer - tgt_layer)
                    seg3_col, seg3_layer = 0, 0
                    end_pos = (tgt_col, tgt_layer)

                elif task == 1:  # 出库:start -> target -> dock
                    seg1_col = abs(start_col - tgt_col)
                    seg1_layer = abs(start_layer - tgt_layer)
                    seg2_col = abs(tgt_col - dock_col)
                    seg2_layer = abs(tgt_layer - dock_layer)
                    seg3_col, seg3_layer = 0, 0
                    end_pos = (dock_col, dock_layer)

                elif task == 3:  # 移库出库
                    seg1_col = abs(start_col - tgt_col)
                    seg1_layer = abs(start_layer - tgt_layer)
                    seg2_col = abs(tgt_col - dock_out_col)
                    seg2_layer = abs(tgt_layer - default_dock_layer)
                    seg3_col, seg3_layer = 0, 0
                    end_pos = (dock_out_col, default_dock_layer)

                elif task == 4:  # 移库入库
                    seg1_col = abs(start_col - dock_in_col)
                    seg1_layer = abs(start_layer - default_dock_layer)
                    seg2_col = abs(dock_in_col - tgt_col)
                    seg2_layer = abs(default_dock_layer - tgt_layer)
                    seg3_col, seg3_layer = 0, 0
                    end_pos = (tgt_col, tgt_layer)

                else:
                    continue

                # === 计算移动时间 ===
                move_sec = run_sec - pick_drop_time if not pd.isna(run_sec) else np.nan

                recs.append({
                    "InstructionID": int(row["InstructionID"]),
                    "OUTorIN": int(task),
                    "StartTime": row["StartTime"],
                    "QROrder": qrorder,
                    "GL_Roadway": int(roadway),
                    "GL_Row": int(tgt_row),
                    "raw_layer": raw_layer if raw_layer is not None else np.nan,
                    "起始列": int(start_col),
                    "起始层": int(start_layer),
                    "目标列": int(tgt_col),
                    "目标层": int(tgt_layer),
                    "Δ列1": int(seg1_col),
                    "Δ层1": int(seg1_layer),
                    "Δ列2": int(seg2_col),
                    "Δ层2": int(seg2_layer),
                    "Δ列3": int(seg3_col),
                    "Δ层3": int(seg3_layer),
                    "取放时间_sec": float(pick_drop_time),
                    "移动时间_sec": float(move_sec) if not pd.isna(move_sec) else np.nan,
                    "总时间": float(run_sec) if not pd.isna(run_sec) else np.nan
                })

                prev_end = end_pos
                last_qrorder = qrorder
                last_seg1 = (seg1_col, seg1_layer)
                last_seg2 = (seg2_col, seg2_layer)

        return pd.DataFrame(recs)
    
    def _travel_time_1d(self, d: float, v_max: float, a: float) -> float:
        """
        单方向加速-匀速-减速时间计算
        d: 距离 (m)
        v_max: 最大速度 (m/s)
        a: 加速度 (m/s^2)
        """
        if d <= 0:
            return 0.0

        t_acc = v_max / a
        d_acc = 0.5 * a * t_acc**2  # 加速段距离
        if d <= 2 * d_acc:
            # 达不到 vmax，三角速度曲线
            return 2 * sqrt(d / a)
        else:
            # 达到 vmax，梯形速度曲线
            return 2 * t_acc + (d - 2 * d_acc) / v_max
    
    def _physics_time_2d(self, delta_col: float, delta_layer: float,
                    col_scale=15.0, layer_scale=0.5,
                    v_col_max=1.6, v_layer_max=0.6,  # 分别定义水平和垂直最大速度
                    a_col=0.2, a_layer=0.4) -> float:
        """
        计算堆垛机在列/层方向的物理最小时间，取两方向时间的 max
        """
        d_col = abs(delta_col) * col_scale
        d_layer = abs(delta_layer) * layer_scale

        # 使用不同方向的最大速度
        t_col = self._travel_time_1d(d_col, v_max=v_col_max, a=a_col)
        t_layer = self._travel_time_1d(d_layer, v_max=v_layer_max, a=a_layer)

        return max(t_col, t_layer)

    def _augment_sequence_physics2d(self, df_seq: pd.DataFrame,
                                col_scale=15.0, layer_scale=0.5,
                                v_col_max=1.5, v_layer_max=0.625,
                                a_col=0.15, a_layer=0.075):
        """
        在 df_seq 中添加列/层独立距离和物理基线时间特征
        """
        df_seq = df_seq.copy()
        df = df_seq.copy().reset_index(drop=True)

        for seg in [1, 2, 3]:
            if f"Δ列{seg}" in df.columns and f"Δ层{seg}" in df.columns:
                df[f"col_dist{seg}"] = df[f"Δ列{seg}"].astype(float) * col_scale
                df[f"layer_dist{seg}"] = df[f"Δ层{seg}"].astype(float) * layer_scale
                df[f"physics_time{seg}"] = df.apply(
                    lambda r: self._physics_time_2d(r[f"Δ列{seg}"], r[f"Δ层{seg}"],
                                            col_scale=col_scale,
                                            layer_scale=layer_scale,
                                            v_col_max=v_col_max,
                                            v_layer_max=v_layer_max,
                                            a_col=a_col,
                                            a_layer=a_layer),
                    axis=1
                )
            else:
                df[f"col_dist{seg}"] = 0.0
                df[f"layer_dist{seg}"] = 0.0
                df[f"physics_time{seg}"] = 0.0

        df["physics_time_total"] = df[["physics_time1", "physics_time2", "physics_time3"]].sum(axis=1)

        df_seq.loc[:, "physics_time_total"] = df["physics_time_total"].values
        df_seq.loc[:, "residual"] = (df_seq["移动时间_sec"] - df["physics_time_total"]).values

        return df_seq
    

    # -------------- 更多可扩展的方法 --------------
    def update_with_new_data(self, new_data: pd.DataFrame, task_type: str = "inbound"):
        """
        占位：接收新数据（历史任务），可以用于后续微调/增量训练 residual 模型
        """
        # 这里只做接口占位；实际训练逻辑交由调用者或单独训练脚本实现
        print(f"[INFO] Received {len(new_data)} records to update {task_type} model. Implement training externally.")


