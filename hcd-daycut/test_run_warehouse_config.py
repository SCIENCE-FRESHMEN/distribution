from types import SimpleNamespace

import run


def test_main_uses_custom_warehouse_config(monkeypatch, tmp_path):
    config_path = tmp_path / "warehouse_custom.json"
    config_path.write_text(
        (
            "{"
            "\"num_aisles\": 10, "
            "\"num_production_lines\": 4, "
            "\"use_magnetic_crane\": false, "
            "\"outbound_congestion_time\": 12.5, "
            "\"lr_balance_weight\": 0.25, "
            "\"initial_inventory_count\": 123"
            "}"
        ),
        encoding="utf-8",
    )

    captured = {}

    class FakeSimulation:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs
            self.warehouse_core = SimpleNamespace(
                initial_inventory_count=kwargs["initial_inventory_count"],
                makespan_weight=None,
                balance_weight=None,
                production_line_avg_time_weight=None,
                production_line_balance_weight=None,
                aisle_dispersion_weight=None,
                inbound_wait_weight=None,
            )

        def run_simulation(self, **kwargs):
            captured["run_kwargs"] = kwargs

    monkeypatch.setattr(run, "WarehouseSimulation", FakeSimulation)
    monkeypatch.setattr(
        run.ProductionPlanBuilder,
        "load_json",
        lambda path: SimpleNamespace(production_plan={}, creation_times={}),
    )

    run.main(
        random_seed=7,
        max_simulation_time=5.0,
        scheduler_type="heuristic",
        warehouse_config_path=str(config_path),
    )

    init_kwargs = captured["init_kwargs"]
    assert init_kwargs["config_path"] == str(config_path)
    assert init_kwargs["num_aisles"] == 10
    assert init_kwargs["num_production_lines"] == 4
    assert init_kwargs["use_magnetic_crane"] is False
    assert init_kwargs["outbound_congestion_time"] == 12.5
    assert init_kwargs["lr_balance_weight"] == 0.25
    assert init_kwargs["initial_inventory_count"] == 123
    assert captured["run_kwargs"]["max_simulation_time"] == 5.0
