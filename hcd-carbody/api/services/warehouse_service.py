
from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from simulation.position import InventoryPosition
from simulation.task_data import TASK_TYPE_INBOUND, TASK_TYPE_OUTBOUND, TaskData
from simulation.warehouse_core import WarehouseCore

project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


class WarehouseService:
    _aisle_availability: Dict[int, Dict[str, Any]] = {}

    def __init__(self, warehouse_core: Optional[WarehouseCore] = None):
        if warehouse_core is None:
            self._core = WarehouseCore(
                scheduler_type="optimization",
                inbound_aisle_strategy="proposed",
                inbound_allocation_strategy="proposed",
                config_path="config/warehouse.json",
            )
            self._core.initialize()
        else:
            self._core = warehouse_core

        self._start_time = time.time()
        self._last_sync_time = self._start_time
        self._pending_execution_tasks: Dict[str, TaskData] = {}

    @property
    def core(self) -> WarehouseCore:
        return self._core

    def _get_current_time(self) -> float:
        return time.time() - self._start_time

    def _sync_time(self) -> None:
        self._core.current_time = self._get_current_time()
        self._last_sync_time = time.time()

    def _sku_entry_to_dict(self, sku: Any) -> Dict[str, Any]:
        if isinstance(sku, dict):
            d = dict(sku)
        elif hasattr(sku, "model_dump"):
            d = sku.model_dump()
        elif hasattr(sku, "dict"):
            d = sku.dict()
        else:
            d = {"skuId": getattr(sku, "skuId", None), "quantity": getattr(sku, "quantity", 1)}

        features = d.get("features") if isinstance(d.get("features"), dict) else {}
        # Allow passing features either in `features` object or as sku top-level fields.
        for k, v in d.items():
            if k in ("skuId", "quantity", "features"):
                continue
            if v is not None and k not in features:
                features[k] = v
        if features:
            d["features"] = self._normalize_features(features)
        return d

    def _normalize_features(self, features: Any) -> Dict[str, Any]:
        if not isinstance(features, dict):
            return {}
        normalizer = getattr(self._core, "_normalize_feature_dict", None)
        if callable(normalizer):
            try:
                out = normalizer(features)
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
        return {str(k): v for k, v in features.items() if v is not None}

    def _extract_sku_features(self, sku_dict: Dict[str, Any], feature_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        features = sku_dict.get("features")
        if isinstance(features, dict):
            return self._normalize_features(dict(features))
        if not feature_keys:
            return {}
        raw = {k: sku_dict.get(k) for k in feature_keys if sku_dict.get(k) is not None}
        return self._normalize_features(raw)

    def _get_match_fields(self, production_line: Optional[int] = None) -> List[str]:
        return list(self._core._get_outbound_match_features(production_line) or [])

    def _normalize_line_id(self, value: Any) -> Optional[int]:
        return self._to_int_or_none(value)

    def _build_core_production_plan(self, production_plan: Any) -> Dict[int, List[Any]]:
        plans = production_plan.plans if hasattr(production_plan, "plans") else production_plan.get("plans", [])
        core_plan: Dict[int, List[Any]] = {}

        for plan in plans or []:
            line_id_raw = plan.lineId if hasattr(plan, "lineId") else plan.get("lineId")
            line_id = self._normalize_line_id(line_id_raw)
            if line_id is None:
                continue

            match_fields = list(self._core._get_outbound_match_features(line_id) or [])
            feature_fields = [f for f in match_fields if str(f).lower() != "rfid"]
            groups = []
            plan_groups = plan.planIndex if hasattr(plan, "planIndex") else plan.get("planIndex", [])
            for group in plan_groups or []:
                tasks_in_group = []
                required_skus = group.requiredSkus if hasattr(group, "requiredSkus") else group.get("requiredSkus", [])
                for task_skus in required_skus or []:
                    sku_list = []
                    for sku in task_skus or []:
                        sku_entry = {"skuId": sku.skuId if hasattr(sku, "skuId") else sku.get("skuId")}
                        quantity = sku.quantity if hasattr(sku, "quantity") else sku.get("quantity", 1)
                        for _ in range(int(quantity or 0)):
                            item = dict(sku_entry)
                            if feature_fields:
                                features = {}
                                for field in feature_fields:
                                    value = getattr(sku, field, None) if hasattr(sku, field) else sku.get(field)
                                    if value is not None:
                                        features[field] = value
                                raw_features = sku.features if hasattr(sku, "features") else sku.get("features")
                                if isinstance(raw_features, dict):
                                    for field in feature_fields:
                                        if field in raw_features and raw_features[field] is not None:
                                            features[field] = raw_features[field]
                                if features:
                                    item["features"] = features
                            sku_list.append(item)
                    tasks_in_group.append(sku_list)
                groups.append(tasks_in_group)
            core_plan[line_id] = groups

        return core_plan

    def _normalize_current_groups(
        self,
        current_groups: Any = None,
        legacy_current_groups: Any = None,
    ) -> Optional[Dict[int, int]]:
        normalized: Dict[int, int] = {}

        if current_groups is not None:
            if isinstance(current_groups, dict):
                rows = [{"lineId": key, "currentGroup": value} for key, value in current_groups.items()]
            else:
                rows = current_groups
            for row in rows or []:
                line_id = self._normalize_line_id(
                    row.lineId if hasattr(row, "lineId") else row.get("lineId")
                )
                group_num = row.currentGroup if hasattr(row, "currentGroup") else row.get("currentGroup")
                if line_id is None or group_num is None:
                    continue
                normalized[line_id] = max(0, int(group_num) - 1)
            return normalized

        if legacy_current_groups is None:
            return None

        rows = legacy_current_groups.items() if isinstance(legacy_current_groups, dict) else []
        for line_key, group_idx in rows:
            line_id = self._normalize_line_id(line_key)
            if line_id is None or group_idx is None:
                continue
            normalized[line_id] = max(0, int(group_idx))
        return normalized
    @staticmethod
    def _to_int_or_none(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, int):
            return int(value)
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        m = re.search(r"LINE[-_]?(\d+)", s.upper())
        if m:
            return int(m.group(1))
        m2 = re.search(r"[lL](\d+)[cC]\d+", s)
        if m2:
            return int(m2.group(1))
        return None

    def _external_to_internal_row(self, external_row: int, aisle: int) -> int:
        return (external_row - 1) % 2 + 1

    def _internal_to_external_row(self, internal_row: int, aisle: int) -> int:
        return 2 * (aisle - 1) + internal_row

    def _parse_line_ref(self, value: Any) -> Optional[Any]:
        """
        Parse line reference from API input.
        Supports:
        - int / numeric string: 1, "2"
        - dock token string: "L4C1", "l1c17"
        Returns normalized int or upper token string.
        """
        if value is None:
            return None
        if isinstance(value, int):
            return int(value)
        s = str(value).strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        m = re.fullmatch(r"[lL]\d+[cC]\d+", s)
        if m:
            return s.upper()
        return s

    def _clamp_col_by_aisle(self, col: int, aisle_id: Optional[int]) -> int:
        if aisle_id is None:
            return int(col)
        try:
            est = getattr(self._core, "time_estimator", None)
            max_col = int(getattr(est, "aisle_max_columns", {}).get(int(aisle_id), 0))
        except Exception:
            max_col = 0
        if max_col <= 0:
            return int(col)
        return max(1, min(int(col), max_col))

    def _normalize_line_ref_key(self, value: Any, aisle_id: Optional[int] = None, direction: Optional[str] = None) -> Optional[Any]:
        line = self._parse_line_ref(value)
        if line is None:
            return None
        if isinstance(line, str):
            s = line.upper()
            m = re.fullmatch(r"[L](\d+)[C](\d+)", s)
            if m:
                level = int(m.group(1))
                col = int(m.group(2))
                col = self._clamp_col_by_aisle(col, aisle_id)
                return f"L{level}C{col}"
            return s
        # Numeric line id: resolve to dock token with aisle-aware clamped column.
        try:
            est = getattr(self._core, "time_estimator", None)
            if direction == "in":
                col, level = est.resolve_inbound_dock(int(line), default_layer=1, aisle=aisle_id)
            elif direction == "out":
                col, level = est.resolve_outbound_dock(int(line), default_layer=1, aisle=aisle_id)
            else:
                return int(line)
            return f"L{int(level)}C{int(col)}"
        except Exception:
            return int(line)

    @staticmethod
    def _normalize_direction(direction: Any) -> Optional[str]:
        if direction is None:
            return None
        s = str(direction).strip().upper()
        if s in ("IN", "INBOUND"):
            return "in"
        if s in ("OUT", "OUTBOUND"):
            return "out"
        return None

    def _is_empty_skid_request(self, sku_list: List[Dict[str, Any]]) -> bool:
        """Detect API outbound request that asks system to choose any empty skid.
        Rule: only check skid_state == 0, skuId can be present or absent.
        """
        if not sku_list:
            return False
        sku = sku_list[0] or {}
        feats = self._normalize_features(sku.get("features") if isinstance(sku.get("features"), dict) else {})
        skid_state = str(feats.get("skid_state", "")).strip()
        if not skid_state:
            canonical = getattr(self._core, "_canonical_feature_key", None)
            for k, v in sku.items():
                if k in ("skuId", "quantity", "features"):
                    continue
                ck = canonical(k) if callable(canonical) else str(k)
                if str(ck) == "skid_state" and v is not None:
                    skid_state = str(v).strip()
                    break
        return skid_state == "0"

    def _extract_position_feature(self, pos: InventoryPosition, keys: List[str]) -> Optional[str]:
        feats = getattr(pos, "features", None)
        if not isinstance(feats, dict):
            return None
        feats = self._normalize_features(feats)
        canonical = getattr(self._core, "_canonical_feature_key", None)
        for k in keys:
            ck = canonical(k) if callable(canonical) else str(k)
            if ck in feats and feats.get(ck) is not None:
                return str(feats.get(ck)).strip()
        return None

    def _empty_skid_positions(self) -> List[InventoryPosition]:
        """Return occupied positions whose feature skid_state indicates empty skid."""
        result: List[InventoryPosition] = []
        for p in self._core.inventory_manager.inventory_positions:
            if p.is_empty():
                continue
            skid_state = self._extract_position_feature(p, ["skid_state", "滑橇状态", "skidState"])
            if skid_state == "0":
                result.append(p)
        return result

    def _is_aisle_idle(self, aisle: int) -> bool:
        return not any(
            (getattr(t, "assigned_aisle", None) == aisle)
            for t in self._core.running_tasks.values()
        )

    def _empty_skid_outbound_score(self, pos: InventoryPosition, out_line: Any, idle_only: bool) -> Tuple[float, float, int]:
        """
        Lower is better:
        1) distance to out dock
        2) projected load (pending inbound + pending outbound in aisle + running in aisle)
        3) aisle id
        """
        dock_col, dock_level = self._core.time_estimator.resolve_outbound_dock(
            out_line, default_layer=1, aisle=pos.aisle
        )
        distance = abs(int(pos.column) - int(dock_col)) + 0.2 * abs(int(pos.level) - int(dock_level))
        running_cnt = sum(
            1 for t in self._core.running_tasks.values()
            if getattr(t, "assigned_aisle", None) == pos.aisle
        )
        pending_in = len(self._core.pending_inbound_by_aisle.get(pos.aisle, []))
        pending_out = sum(
            1 for t in self._core.pending_outbound_queue
            if getattr(t, "assigned_aisle", None) == pos.aisle
        )
        load = float(running_cnt + pending_in + pending_out)
        # If caller asks idle-only and this aisle is busy, make it non-competitive.
        if idle_only and running_cnt > 0:
            load += 1e6
        return (distance, load, int(pos.aisle))

    def _resolve_empty_skid_outbound(self, task: TaskData) -> Optional[TaskData]:
        """
        Convert placeholder empty-skid outbound request into concrete outbound:
        choose a specific occupied empty-skid position and bind skuId/aisle/position.
        """
        out_line = getattr(task, "out_line", None)
        all_candidates = [
            p
            for p in self._empty_skid_positions()
            if self.is_outbound_path_available(int(p.aisle), out_line)
        ]
        if not all_candidates:
            return None

        available_now = [
            p for p in all_candidates
            if self._is_aisle_idle(int(p.aisle))
            and self.is_outbound_path_available(int(p.aisle), out_line)
            and (not self._core.check_blockage(int(p.aisle), out_line, current_time=self._core.current_time))
        ]
        if available_now:
            chosen = min(available_now, key=lambda p: self._empty_skid_outbound_score(p, out_line, idle_only=True))
        else:
            # All candidate aisles busy or blocked now: choose lower future load.
            chosen = min(all_candidates, key=lambda p: self._empty_skid_outbound_score(p, out_line, idle_only=False))

        sku_id = str(getattr(chosen, "sku", "") or "")
        qty = int(getattr(chosen, "quantity", 1) or 1)
        feats = dict(getattr(chosen, "features", {}) or {})
        task.positions = [chosen]
        task.assigned_aisle = int(chosen.aisle)
        task.skus = [{"skuId": sku_id, "quantity": qty, "features": feats}]
        task.task_record = dict(getattr(task, "task_record", {}) or {})
        task.task_record["empty_skid_request"] = True
        task.task_record["high_priority"] = True
        task.task_record["resolved_by"] = "api_empty_skid_selector"
        return task

    def _rebuild_inventory_views(self) -> None:
        manager = self._core.inventory_manager
        manager.sku_position_index = {}
        manager.current_inventory = {aisle: {} for aisle in self._core.aisles}

        dynamic_skus = set()
        for position in manager.inventory_positions:
            if position.is_double_layer:
                if position.upper_quantity > 0 and position.upper_sku:
                    dynamic_skus.add(position.upper_sku)
                    manager.current_inventory[position.aisle][position.upper_sku] = manager.current_inventory[position.aisle].get(position.upper_sku, 0) + position.upper_quantity
                    manager.sku_position_index.setdefault(position.upper_sku, []).append(position)
                if position.lower_quantity > 0 and position.lower_sku:
                    dynamic_skus.add(position.lower_sku)
                    manager.current_inventory[position.aisle][position.lower_sku] = manager.current_inventory[position.aisle].get(position.lower_sku, 0) + position.lower_quantity
                    manager.sku_position_index.setdefault(position.lower_sku, []).append(position)
            elif position.quantity > 0 and position.sku:
                dynamic_skus.add(position.sku)
                manager.current_inventory[position.aisle][position.sku] = manager.current_inventory[position.aisle].get(position.sku, 0) + position.quantity
                manager.sku_position_index.setdefault(position.sku, []).append(position)

        self._core.sku_types = sorted(dynamic_skus)
        manager.sku_types = sorted(dynamic_skus)

    def sync_aisle_status(self, aisle_status_list: List[Any]) -> None:
        self._sync_time()
        for status in aisle_status_list:
            aisle_id = int(status.aisleId) if hasattr(status, "aisleId") else int(status["aisleId"])
            is_available = status.isAvailable if hasattr(status, "isAvailable") else status["isAvailable"]
            dock_availability = {"in": {}, "out": {}}
            dock_rows = status.dockAvailability if hasattr(status, "dockAvailability") else status.get("dockAvailability", [])
            for row in dock_rows or []:
                direction = row.direction if hasattr(row, "direction") else row.get("direction")
                direction_norm = self._normalize_direction(direction)
                line_ref = row.lineRef if hasattr(row, "lineRef") else row.get("lineRef")
                line_key = self._normalize_line_ref_key(line_ref, aisle_id=aisle_id, direction=direction_norm)
                available = row.isAvailable if hasattr(row, "isAvailable") else row.get("isAvailable", True)
                if direction_norm and line_key is not None:
                    dock_availability[direction_norm][line_key] = bool(available)
            self._aisle_availability[aisle_id] = {
                "is_available": is_available,
                "unavailable_reason": status.unavailableReason if hasattr(status, "unavailableReason") else status.get("unavailableReason"),
                "bank": status.bank if hasattr(status, "bank") else status.get("bank"),
                "dock_availability": dock_availability,
            }

            if not is_available:
                for out_line in range(1, self._core.num_production_lines + 1):
                    self._core.update_blockage_status(
                        aisle=aisle_id,
                        out_line=out_line,
                        blocked=True,
                        unblock_time=self._core.current_time + self._core.outbound_congestion_time,
                    )
                continue

            exit_congestion = status.exitCongestion if hasattr(status, "exitCongestion") else status.get("exitCongestion", [])
            for congestion in exit_congestion:
                line_id_str = congestion.lineId if hasattr(congestion, "lineId") else congestion["lineId"]
                line_id = int(str(line_id_str).replace("LINE-", ""))
                is_congested = congestion.isCongested if hasattr(congestion, "isCongested") else congestion["isCongested"]
                self._core.update_blockage_status(
                    aisle=aisle_id,
                    out_line=line_id,
                    blocked=bool(is_congested),
                    unblock_time=(self._core.current_time + self._core.outbound_congestion_time) if is_congested else 0.0,
                )

    def sync_inventory(self, inventory_list: List[Any]) -> None:
        self._sync_time()
        if not inventory_list:
            return

        if len(inventory_list) >= 15:
            self._clear_all_inventory()

        feature_keys = self._get_match_fields()
        for inv_item in inventory_list:
            aisle_id = int(inv_item.aisleId) if hasattr(inv_item, "aisleId") else int(inv_item["aisleId"])
            external_row = inv_item.row if hasattr(inv_item, "row") else inv_item["row"]
            column = inv_item.column if hasattr(inv_item, "column") else inv_item["column"]
            level = inv_item.level if hasattr(inv_item, "level") else inv_item["level"]
            shelf = inv_item.shelf if hasattr(inv_item, "shelf") else inv_item.get("shelf")
            positions_data = inv_item.positions if hasattr(inv_item, "positions") else inv_item.get("positions", [])

            internal_row = self._external_to_internal_row(external_row, aisle_id)
            position_id = f"{aisle_id:01d}-{internal_row:01d}-{column:02d}-{level:02d}"
            position = self._core.inventory_manager.position_map.get(position_id)
            if position is None:
                continue

            if position.is_double_layer:
                position.upper_sku = None
                position.upper_quantity = 0
                position.upper_features = None
                position.lower_sku = None
                position.lower_quantity = 0
                position.lower_features = None
            else:
                position.sku = ""
                position.quantity = 0
                position.features = None

            for pos_data in (positions_data or []):
                sku_dict = self._sku_entry_to_dict(pos_data)
                sku_id = sku_dict.get("skuId")
                quantity = sku_dict.get("quantity", 0)
                if not sku_id or quantity <= 0:
                    continue
                sku_features = self._extract_sku_features(sku_dict, feature_keys)

                if position.is_double_layer:
                    shelf_str = str(shelf).upper() if shelf else None
                    if shelf_str and "UPPER" in shelf_str:
                        position.upper_sku = sku_id
                        position.upper_quantity = quantity
                        position.upper_features = sku_features
                    elif shelf_str and "LOWER" in shelf_str:
                        position.lower_sku = sku_id
                        position.lower_quantity = quantity
                        position.lower_features = sku_features
                    else:
                        if position.upper_quantity == 0:
                            position.upper_sku = sku_id
                            position.upper_quantity = quantity
                            position.upper_features = sku_features
                        else:
                            position.lower_sku = sku_id
                            position.lower_quantity = quantity
                            position.lower_features = sku_features
                else:
                    position.sku = sku_id
                    position.quantity = quantity
                    position.features = sku_features

        self._rebuild_inventory_views()
    def _clear_all_inventory(self) -> None:
        for position in self._core.inventory_manager.inventory_positions:
            if position.is_double_layer:
                position.upper_sku = None
                position.upper_quantity = 0
                position.upper_features = None
                position.lower_sku = None
                position.lower_quantity = 0
                position.lower_features = None
            else:
                position.sku = ""
                position.quantity = 0
                position.features = None

        self._core.inventory_manager.current_inventory = {aisle: {} for aisle in self._core.aisles}
        self._core.inventory_manager.sku_position_index = {}
        self._core.sku_types = []
        self._core.inventory_manager.sku_types = []
        self._core.running_tasks.clear()
        self._core.completed_tasks.clear()
        self._core.pending_outbound_queue.clear()
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle].clear()
        self._pending_execution_tasks.clear()
        for aisle in self._core.aisles:
            self._core.current_position_by_aisle[aisle] = None

    def is_aisle_available(self, aisle_id: int) -> bool:
        return self._aisle_availability.get(aisle_id, {}).get("is_available", True)

    def _is_path_available(self, aisle_id: int, line_ref: Any, direction: str) -> bool:
        if not self.is_aisle_available(aisle_id):
            return False
        direction_norm = self._normalize_direction(direction)
        if direction_norm not in ("in", "out"):
            return True
        cfg = self._aisle_availability.get(aisle_id, {})
        dock_cfg = cfg.get("dock_availability", {}) or {}
        dir_map = dock_cfg.get(direction_norm, {}) or {}
        line_key = self._normalize_line_ref_key(line_ref, aisle_id=aisle_id, direction=direction_norm)
        if line_key is None:
            return True
        if line_key in dir_map:
            return bool(dir_map[line_key])
        # Fallback: if one side uses numeric and another uses token, try numeric extraction.
        line_num = self._to_int_or_none(line_key)
        if line_num is not None and line_num in dir_map:
            return bool(dir_map[line_num])
        return True

    def is_inbound_path_available(self, aisle_id: int, in_line: Any) -> bool:
        return self._is_path_available(aisle_id, in_line, "inbound")

    def is_outbound_path_available(self, aisle_id: int, out_line: Any) -> bool:
        return self._is_path_available(aisle_id, out_line, "outbound")

    def convert_schedule_tasks(self, tasks: List[Any]) -> Tuple[List[TaskData], List[TaskData]]:
        inbound_tasks: List[TaskData] = []
        outbound_tasks: List[TaskData] = []

        for task in tasks:
            def _get_field(obj: Any, name: str, default: Any = None) -> Any:
                if isinstance(obj, dict):
                    return obj.get(name, default)
                return getattr(obj, name, default)

            task_id = _get_field(task, "taskId")
            task_type = _get_field(task, "taskType")
            skus = _get_field(task, "skus", [])

            sku_list = []
            for sku in skus:
                sku_dict = self._sku_entry_to_dict(sku)
                sku_dict["skuId"] = sku_dict.get("skuId") or sku_dict.get("sku")
                sku_dict["quantity"] = sku_dict.get("quantity", 1)
                sku_list.append(sku_dict)

            if "INBOUND" in str(task_type).upper():
                target_aisle = _get_field(task, "targetAisle")
                in_line_raw = _get_field(task, "inLine")
                out_line_raw = _get_field(task, "outLine")
                production_line_raw = _get_field(task, "productionLine")
                in_line = self._parse_line_ref(in_line_raw)
                out_line = self._parse_line_ref(out_line_raw)
                production_line = int(production_line_raw) if production_line_raw is not None else 1
                inbound_tasks.append(
                    TaskData(
                        task_id=task_id,
                        task_type=TASK_TYPE_INBOUND,
                        task_name=task_id,
                        skus=sku_list,
                        assigned_aisle=int(target_aisle) if target_aisle else None,
                        in_line=(in_line if in_line is not None else 1),
                        out_line=out_line,
                        production_line=production_line,
                    )
                )
                continue

            plan_id = _get_field(task, "planId")
            plan_index = _get_field(task, "planIndex")
            out_line = _get_field(task, "outLine")
            production_line_raw = _get_field(task, "productionLine")
            out_line = self._parse_line_ref(out_line)
            production_line = int(production_line_raw) if production_line_raw is not None else None
            if plan_id:
                plan_str = str(plan_id).upper()
                if "LINE" in plan_str:
                    # Extract only the production line number after "LINE",
                    # avoid mixing in date digits from values like PLAN-LINE1-20260121.
                    m = re.search(r"LINE[-_]?(\d+)", plan_str)
                    if m:
                        production_line = int(m.group(1))
                elif str(plan_id).isdigit():
                    production_line = int(plan_id)
            if production_line is None and sku_list:
                first_sku = sku_list[0].get("skuId", "")
                pl_value = self._core.sku_to_production_line.get(first_sku, 1)
                production_line = int(pl_value[0]) if isinstance(pl_value, list) and pl_value else int(pl_value or 1)
            if production_line is None:
                production_line = self._to_int_or_none(out_line)
            if production_line is None and plan_id:
                production_line = self._to_int_or_none(plan_id)
            if production_line is None:
                production_line = 1

            task_data = TaskData(task_id=task_id, task_type=TASK_TYPE_OUTBOUND, task_name=task_id, skus=sku_list, production_line=production_line or 1)
            if out_line is not None:
                task_data.out_line = out_line
            task_data.plan_id = plan_id
            task_data.group_idx = (int(plan_index) - 1) if plan_index is not None else None
            if self._is_empty_skid_request(sku_list):
                # Empty-skid outbound is an ad-hoc operational request rather than
                # a production-plan step: do not bind it to plan/group progression.
                task_data.plan_id = None
                task_data.group_idx = None
                task_data.task_record = {
                    "empty_skid_request": True,
                    "high_priority": True,
                }
            outbound_tasks.append(task_data)

        return inbound_tasks, outbound_tasks

    def _find_positions_for_outbound_task(self, task: TaskData) -> Optional[List[InventoryPosition]]:
        sku_ids = task.get_sku_ids() if hasattr(task, "get_sku_ids") else []
        if not sku_ids:
            for s in (task.skus or []):
                sid = s.get("skuId") if isinstance(s, dict) else getattr(s, "skuId", None)
                if sid:
                    sku_ids.append(sid)
        if not sku_ids:
            return None

        production_line = task.production_line or 1
        out_line = getattr(task, "out_line", None) or production_line
        match_mode = self._core._get_outbound_match_mode(production_line)
        feature_keys = self._get_match_fields(production_line)
        sku_features_by_idx = [self._extract_sku_features(self._sku_entry_to_dict(s), feature_keys) for s in (task.skus or [])]

        def can_use(pos: InventoryPosition) -> bool:
            return (
                self.is_outbound_path_available(pos.aisle, out_line)
                and not self._core.check_blockage(pos.aisle, out_line, current_time=self._core.current_time)
            )

        if len(sku_ids) == 1:
            sku = sku_ids[0]
            feats = sku_features_by_idx[0] if sku_features_by_idx else {}
            if match_mode == "features" and feats:
                candidates = self._core.inventory_manager.get_positions_by_features(feats, feature_keys, only_available=True)
            else:
                candidates = self._core.inventory_manager.get_sku_positions(sku, only_available=True)
            return next(([p] for p in candidates if can_use(p)), None)

        sku1, sku2 = sku_ids[0], sku_ids[1]
        feats1 = sku_features_by_idx[0] if len(sku_features_by_idx) > 0 else {}
        feats2 = sku_features_by_idx[1] if len(sku_features_by_idx) > 1 else {}

        for pos in self._core.inventory_manager.inventory_positions:
            if not pos.is_double_layer or not can_use(pos):
                continue
            if match_mode == "features" and feats1 and feats2:
                up1 = pos.upper_quantity > 0 and self._core.inventory_manager._features_match(pos.upper_features, feats1, feature_keys)
                low2 = pos.lower_quantity > 0 and self._core.inventory_manager._features_match(pos.lower_features, feats2, feature_keys)
                up2 = pos.upper_quantity > 0 and self._core.inventory_manager._features_match(pos.upper_features, feats2, feature_keys)
                low1 = pos.lower_quantity > 0 and self._core.inventory_manager._features_match(pos.lower_features, feats1, feature_keys)
                if (up1 and low2) or (up2 and low1):
                    return [pos]
            else:
                has1 = (pos.upper_sku == sku1 and pos.upper_quantity > 0) or (pos.lower_sku == sku1 and pos.lower_quantity > 0)
                has2 = (pos.upper_sku == sku2 and pos.upper_quantity > 0) or (pos.lower_sku == sku2 and pos.lower_quantity > 0)
                if has1 and has2:
                    return [pos]

        p1 = self._core.inventory_manager.get_positions_by_features(feats1, feature_keys, only_available=True) if (match_mode == "features" and feats1) else self._core.inventory_manager.get_sku_positions(sku1, only_available=True)
        p2 = self._core.inventory_manager.get_positions_by_features(feats2, feature_keys, only_available=True) if (match_mode == "features" and feats2) else self._core.inventory_manager.get_sku_positions(sku2, only_available=True)
        for a in p1:
            for b in p2:
                if can_use(a) and can_use(b):
                    return [a, b]
        return None

    def execute_schedule(self, tasks: Tuple[List[TaskData], List[TaskData]]) -> Dict[int, Optional[TaskData]]:
        self._sync_time()
        inbound_tasks, outbound_tasks = tasks

        ready_outbound: List[TaskData] = []
        for task in outbound_tasks:
            if bool((getattr(task, "task_record", {}) or {}).get("empty_skid_request")):
                task = self._resolve_empty_skid_outbound(task) or task
            if not getattr(task, "positions", None):
                task.positions = self._find_positions_for_outbound_task(task) or []
            if task.positions:
                ready_outbound.append(task)

        for task in inbound_tasks:
            if not task.assigned_aisle:
                continue
            aisle = int(task.assigned_aisle)
            in_line = getattr(task, "in_line", None)
            production_line = getattr(task, "production_line", None)
            valid_aisles = self._core._get_valid_inbound_aisles(task, production_line)
            if aisle not in valid_aisles:
                candidates = [a for a in valid_aisles if self.is_inbound_path_available(a, in_line)]
                if candidates:
                    aisle = min(candidates, key=lambda a: len(self._core.pending_inbound_by_aisle.get(a, [])))
                    task.assigned_aisle = aisle
                else:
                    continue
            if not self.is_inbound_path_available(aisle, in_line):
                continue
            existing_ids = {t.task_id for t in self._core.pending_inbound_by_aisle.get(aisle, [])}
            if task.task_id not in existing_ids:
                self._core.pending_inbound_by_aisle[aisle].append(task)

        # Keep empty-skid high-priority outbound tasks at queue front.
        existing_outbound = {t.task_id for t in self._core.pending_outbound_queue}
        normal_tasks: List[TaskData] = []
        priority_tasks: List[TaskData] = []
        for task in ready_outbound:
            if task.task_id in existing_outbound:
                continue
            rec = getattr(task, "task_record", {}) or {}
            if bool(rec.get("high_priority")) and bool(rec.get("empty_skid_request")):
                priority_tasks.append(task)
            else:
                normal_tasks.append(task)
        if priority_tasks:
            self._core.pending_outbound_queue = priority_tasks + self._core.pending_outbound_queue
        if normal_tasks:
            self._core.pending_outbound_queue.extend(normal_tasks)

        inbound_for_schedule: List[TaskData] = []
        for aisle in self._core.aisles:
            line_buckets: Dict[int, TaskData] = {}
            for t in self._core.pending_inbound_by_aisle.get(aisle, []):
                line = getattr(t, "in_line", 1)
                if line not in line_buckets:
                    line_buckets[line] = t
            inbound_for_schedule.extend(line_buckets.values())

        aisle_task_sequences = self._core.scheduler.solve(
            inbound_tasks=inbound_for_schedule,
            outbound_tasks=list(self._core.pending_outbound_queue),
            running_tasks=self._core.running_tasks,
            current_time=self._core.current_time,
        )

        result: Dict[int, Optional[TaskData]] = {}
        busy_aisles = {t.assigned_aisle for t in self._core.running_tasks.values() if getattr(t, "assigned_aisle", None)}
        for aisle in self._core.aisles:
            if not self.is_aisle_available(aisle):
                result[aisle] = None
                continue
            if aisle in busy_aisles:
                result[aisle] = next((t for t in self._core.running_tasks.values() if t.assigned_aisle == aisle), None)
                continue

            sequence = aisle_task_sequences.get(aisle, [])
            if not sequence:
                result[aisle] = None
                continue

            task = sequence[0]
            if task.task_type == TASK_TYPE_OUTBOUND and task.production_line is not None:
                out_line = getattr(task, "out_line", None) or task.production_line
                if not self.is_outbound_path_available(aisle, out_line):
                    result[aisle] = None
                    continue
                if self._core.check_blockage(aisle, out_line, current_time=self._core.current_time):
                    result[aisle] = None
                    continue
                if not self._core.can_start_outbound_task(task.task_id, task.production_line):
                    result[aisle] = None
                    continue

            if not getattr(task, "positions", None):
                result[aisle] = None
                continue

            task.assigned_aisle = aisle
            task.task_record = self._core.generate_task_record(task, self._core.current_time)
            self._pending_execution_tasks[task.task_id] = task
            result[aisle] = task

        return result

    def allocate_inbound_aisle(
        self,
        task_id: str,
        skus: List[Dict],
        in_line: Any = 1,
        out_line: Any = None,
        production_line: Any = None,
    ) -> int:
        self._sync_time()
        production_line_val = int(production_line) if production_line is not None else None
        if production_line_val is None:
            for sku in (skus or []):
                sku_id = self._sku_entry_to_dict(sku).get("skuId")
                if not sku_id:
                    continue
                pl_value = self._core.sku_to_production_line.get(sku_id)
                if isinstance(pl_value, list):
                    production_line_val = int(pl_value[0]) if pl_value else None
                elif pl_value is not None:
                    production_line_val = int(pl_value)
                if production_line_val:
                    break

        in_line_norm = self._parse_line_ref(in_line)
        out_line_norm = self._parse_line_ref(out_line)
        stub = type(
            "TaskStub",
            (),
            {
                "skus": skus,
                "in_line": (in_line_norm if in_line_norm is not None else 1),
                "out_line": out_line_norm,
                "assigned_aisle": None,
                "production_line": production_line_val,
            },
        )()
        valid_aisles = self._core._get_valid_inbound_aisles(stub, production_line_val)
        valid_aisles = [a for a in valid_aisles if self.is_inbound_path_available(a, in_line_norm)]
        valid_with_capacity = [a for a in valid_aisles if self._core._get_projected_free_slots(a) > 0]
        if self._core.inbound_aisle_allocator is not None:
            try:
                aisle = self._core.inbound_aisle_allocator.allocate(stub, self._core.inventory_manager.inventory_positions)
                if aisle:
                    aisle = int(aisle)
                    if aisle in valid_aisles:
                        return aisle
            except Exception:
                pass

        if valid_with_capacity:
            return min(valid_with_capacity, key=lambda a: len(self._core.pending_inbound_by_aisle.get(a, [])))
        if valid_aisles:
            return min(valid_aisles, key=lambda a: len(self._core.pending_inbound_by_aisle.get(a, [])))
        if production_line_val is not None:
            pl_aisles = [a for a in self._core.aisles if production_line_val in self._core.aisle_production_line_mapping.get(a, [])]
            pl_aisles = [a for a in pl_aisles if self.is_aisle_available(a)]
            if pl_aisles:
                return random.choice(pl_aisles)
        return random.choice([a for a in self._core.aisles if self.is_aisle_available(a)] or self._core.aisles)

    def apply_feedback(self, feedback: Dict[str, Any]) -> bool:
        self._sync_time()
        task_id = feedback.get("taskId")
        status = str(feedback.get("status", "")).upper()
        task_type = str(feedback.get("taskType", "")).upper()
        if not task_id:
            return False

        self._core.apply_task_feedback(feedback)
        if status == "EXECUTING":
            return self._start_task_execution(task_id, task_type)
        if status == "COMPLETED":
            return self._complete_task_execution(task_id)
        if status == "FAILED":
            return self._fail_task_execution(task_id)
        return True

    def _start_task_execution(self, task_id: str, task_type: str) -> bool:
        task = self._pending_execution_tasks.pop(task_id, None)
        if task is None:
            if task_type == "OUTBOUND":
                task = next((t for t in self._core.pending_outbound_queue if t.task_id == task_id), None)
            else:
                for queue in self._core.pending_inbound_by_aisle.values():
                    task = next((t for t in queue if t.task_id == task_id), None)
                    if task:
                        break
        if task is None:
            return task_id in self._core.running_tasks

        if task_type == "OUTBOUND":
            self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
        else:
            for aisle in list(self._core.pending_inbound_by_aisle.keys()):
                self._core.pending_inbound_by_aisle[aisle] = [
                    t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id
                ]

        if not getattr(task, "task_record", None):
            task.task_record = self._core.generate_task_record(task, self._core.current_time)
        self._core.running_tasks[task_id] = task
        return True

    def _complete_task_execution(self, task_id: str) -> bool:
        self._pending_execution_tasks.pop(task_id, None)
        task = self._core.running_tasks.pop(task_id, None)
        self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle] = [t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id]
        if task is None:
            return False
        self._core.completed_tasks.append(task)
        if getattr(task, "positions", None) and task.assigned_aisle is not None:
            self._core.current_position_by_aisle[task.assigned_aisle] = task.positions[-1]
        return True

    def _fail_task_execution(self, task_id: str) -> bool:
        self._pending_execution_tasks.pop(task_id, None)
        self._core.running_tasks.pop(task_id, None)
        self._core.pending_outbound_queue = [t for t in self._core.pending_outbound_queue if t.task_id != task_id]
        for aisle in list(self._core.pending_inbound_by_aisle.keys()):
            self._core.pending_inbound_by_aisle[aisle] = [t for t in self._core.pending_inbound_by_aisle[aisle] if t.task_id != task_id]
        return True

    def set_production_plan(
        self,
        production_plan: Any,
        update: bool = False,
        current_groups: Any = None,
        legacy_current_groups: Any = None,
    ) -> bool:
        self._sync_time()
        try:
            if isinstance(production_plan, dict) and "production_plan" in production_plan:
                core_plan = production_plan.get("production_plan", {}) or {}
            elif isinstance(production_plan, dict) and "plans" in production_plan:
                core_plan = self._build_core_production_plan(production_plan)
            elif hasattr(production_plan, "plans"):
                core_plan = self._build_core_production_plan(production_plan)
            else:
                core_plan = production_plan or {}

            current_group_map = self._normalize_current_groups(current_groups, legacy_current_groups)
            self._core.set_production_plan(core_plan, current_groups=current_group_map)
            return True
        except Exception:
            return False

    def set_current_groups(self, current_groups: Any = None, legacy_current_groups: Any = None) -> bool:
        self._sync_time()
        current_group_map = self._normalize_current_groups(current_groups, legacy_current_groups)
        if current_group_map is None:
            return True
        for line_id, group_idx in current_group_map.items():
            group_count = len(self._core.production_plan.get(line_id, []) or [])
            self._core.production_line_current_group[line_id] = max(0, min(int(group_idx), group_count))
            self._core.production_line_completed_tasks[line_id] = set()
            self._core.production_line_group_completion_times[line_id] = []
        return True

    def get_production_plan(self) -> Dict[int, List]:
        return self._core.production_plan

    def get_running_tasks(self) -> Dict[str, TaskData]:
        return self._core.running_tasks.copy()

    def get_pending_tasks(self) -> Dict[str, List[TaskData]]:
        return {
            "inbound": {aisle: list(tasks) for aisle, tasks in self._core.pending_inbound_by_aisle.items()},
            "outbound": list(self._core.pending_outbound_queue),
        }

    def get_completed_tasks(self) -> List[TaskData]:
        return list(self._core.completed_tasks)

    def get_inventory_summary(self) -> Dict[int, Dict[str, int]]:
        return {aisle: {sku: qty for sku, qty in skus.items() if qty > 0} for aisle, skus in self._core.inventory_manager.current_inventory.items()}

    def get_full_inventory(self) -> List[Dict[str, Any]]:
        full_inventory: List[Dict[str, Any]] = []
        match_fields = self._get_match_fields()
        for position in self._core.inventory_manager.inventory_positions:
            base_info = {
                "aisleId": str(position.aisle),
                "row": self._internal_to_external_row(position.row, position.aisle),
                "column": position.column,
                "level": position.level,
            }
            if position.is_double_layer:
                upper_entry = {"skuId": position.upper_sku or "", "quantity": position.upper_quantity or 0}
                lower_entry = {"skuId": position.lower_sku or "", "quantity": position.lower_quantity or 0}
                upper_features = getattr(position, "upper_features", {}) or {}
                lower_features = getattr(position, "lower_features", {}) or {}
                for field in match_fields:
                    if field in upper_features:
                        upper_entry[field] = upper_features[field]
                    if field in lower_features:
                        lower_entry[field] = lower_features[field]
                full_inventory.append({**base_info, "positions": [upper_entry, lower_entry]})
            else:
                entry = {"skuId": getattr(position, "sku", "") or "", "quantity": getattr(position, "quantity", 0) or 0}
                features = getattr(position, "features", {}) or {}
                for field in match_fields:
                    if field in features:
                        entry[field] = features[field]
                full_inventory.append({**base_info, "positions": [entry]})
        return full_inventory

    def get_aisle_status(self) -> Dict[int, Dict[str, Any]]:
        result: Dict[int, Dict[str, Any]] = {}
        for aisle in self._core.aisles:
            is_busy = any(t.assigned_aisle == aisle for t in self._core.running_tasks.values())
            blockage = {}
            for pl in range(1, self._core.num_production_lines + 1):
                status = self._core.blockage_status.get((aisle, pl), {})
                unblock_time = status.get("unblock_time", 0.0)
                if unblock_time == float("inf"):
                    unblock_time = -1
                blockage[pl] = {"blocked": status.get("blocked", False), "unblock_time": unblock_time}
            current_position = self._core.current_position_by_aisle.get(aisle)
            if current_position is not None:
                current_position_data = {
                    "aisle": int(getattr(current_position, "aisle", aisle)),
                    "row": int(getattr(current_position, "row", 0) or 0),
                    "column": int(getattr(current_position, "column", 0) or 0),
                    "level": int(getattr(current_position, "level", 0) or 0),
                }
            else:
                current_position_data = None
            result[aisle] = {"is_busy": is_busy, "blockage": blockage, "current_position": current_position_data}
        return result

    def update_sku_config(self, config_data: Dict[str, Any]) -> bool:
        required_fields = ["sku_types", "sku_pairs", "sku_solo", "sku_to_production_line"]
        for field in required_fields:
            if field not in config_data:
                raise ValueError(f"Missing required field: {field}")
        config_path = Path(project_root) / "simulation" / "data" / "sku_config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)
        self._core.sku_types = config_data["sku_types"]
        self._core.sku_to_production_line = config_data["sku_to_production_line"]
        return True


_warehouse_service: Optional[WarehouseService] = None


def get_warehouse_service() -> WarehouseService:
    global _warehouse_service
    if _warehouse_service is None:
        _warehouse_service = WarehouseService()
    return _warehouse_service


def init_warehouse_service(warehouse_core: Optional[WarehouseCore] = None) -> WarehouseService:
    global _warehouse_service
    _warehouse_service = WarehouseService(warehouse_core)
    return _warehouse_service


def reset_warehouse_service() -> None:
    global _warehouse_service
    _warehouse_service = None

