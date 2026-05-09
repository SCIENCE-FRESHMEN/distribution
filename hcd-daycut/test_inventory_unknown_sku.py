from simulation.inventory import InventoryManager


def test_add_inventory_registers_unknown_sku():
    manager = InventoryManager(
        num_aisles=1,
        num_rows=1,
        num_columns=1,
        num_levels=1,
        total_positions=1,
        sku_types=["KNOWN"],
        initial_inventory_ratio=0.0,
    )
    manager.initialize()
    position = manager.inventory_positions[0]

    manager.add_inventory(position=position, sku="UNKNOWN", quantity=1, layer="upper")

    assert "UNKNOWN" in manager.sku_types
    assert manager.current_inventory[1]["UNKNOWN"] == 1
