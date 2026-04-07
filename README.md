# Distribution: Huachang Kuda Warehouse Scheduling System
This repository implements a comprehensive warehouse scheduling solution tailored for Huachang Kuda's warehouse operation scenarios, covering end-to-end scheduling logic, resource management, result visualization, and API integration capabilities.

## Core Structure
The repository is organized into two core business submodules with modular, reusable architecture:

### Root Directory
- `.gitattributes`: Git attribute configuration for consistent version control behavior.
- `README.md`: Core documentation (this file) outlining repository purpose and usage.

### 1. hcd-daycut/ (Core Scheduling Module)
The primary module for general warehouse scheduling workflows, containing:
- **Documentation**: `API_Documentation.md` (API specs), `README.md` (module-specific guide).
- **Core Logic**:
  - `allocation/`: Resource allocation (storage locations, equipment, manpower).
  - `api/`: API layer for external system integration.
  - `config/`: Configuration management (environment, scheduling rules).
  - `docs/`: Supplementary technical documentation.
  - `estimate/`: Scheduling time/cost estimation logic.
  - `schedule/`: Core scheduling algorithm and workflow execution.
  - `scripts/`: Auxiliary scripts for data processing/validation.
  - `simulation/`: Scheduling simulation for strategy testing.
- **Execution Scripts**:
  - `run.py`: Main entry point for scheduling execution.
  - `run_api.py`: Start API service for scheduling operations.
  - `run_compare.py`: Compare results of different scheduling strategies/parameters.
  - `run_daily.py`: Automated daily scheduling task execution.
- **Testing & Visualization**:
  - `test_api_flow.py`: End-to-end API flow testing.
  - `plot_gantt_from_log.py`: Generate Gantt charts from scheduling logs.
  - `visualize_daily_results.py`/`visualize_results.py`: Visualize scheduling outcomes for analysis.
- `requirements.txt`: Python dependency list for environment setup.

### 2. hcd-carbody/ (Scenario-Specific Scheduling Module)
Optimized for specialized warehouse scheduling scenarios (e.g., carbody-related inventory/operations), with a structure aligned with `hcd-daycut`:
- **Core Logic**: Reusable `allocation/`, `config/`, `estimate/`, `schedule/`, `scripts/`, `simulation/` directories (consistent with core module).
- **Execution Scripts**: `run.py`, `run_compare.py`, `run_daily.py` (adapted for scenario-specific rules).
- **Visualization**: `plot_gantt_from_log.py` (Gantt chart visualization for scenario-specific logs).
- **Guidance**: `new_warehouse_setup_guide.md` (step-by-step guide for deploying the system to new warehouses).

## Key Capabilities
### 1. Scheduling Execution
- Automated daily scheduling via `run_daily.py`.
- Custom scheduling simulation via `simulation/` and `run.py`.
- Strategy comparison to optimize scheduling rules (`run_compare.py`).

### 2. Resource Management
- Intelligent allocation of warehouse resources (locations, equipment, labor) via `allocation/` module.
- Configurable rules to adapt to dynamic warehouse conditions (`config/`).

### 3. Observability & Visualization
- Gantt chart generation from scheduling logs for intuitive workflow analysis.
- Result visualization scripts to compare performance metrics (time, cost, resource utilization).

### 4. API Integration
- RESTful API layer (`hcd-daycut/api/`) for seamless integration with external WMS/ERP systems.
- End-to-end API testing (`test_api_flow.py`) to ensure reliability.

### 5. Scenario Adaptability
- Two dedicated submodules (`hcd-daycut`, `hcd-carbody`) for different operational scenarios.
- Shared core architecture with independent configuration for easy extension to new scenarios.

## Technical Features
- **Language**: Python-based (compatible with Python 3.8+).
- **Modularity**: Decoupled core logic (scheduling/allocation) from execution/visualization/API layers for maintainability.
- **Observability**: Rich logging, result comparison, and visualization for strategy optimization and troubleshooting.
- **Documentation**: Comprehensive API docs, setup guides, and module readmes to reduce onboarding costs.

## Getting Started
1. Install dependencies:
   ```bash
   cd hcd-daycut
   pip install -r requirements.txt
   ```
2. Configure scheduling rules (modify `config/` directory).
3. Run daily scheduling:
   ```bash
   python run_daily.py
   ```
4. Visualize results:
   ```bash
   python visualize_daily_results.py
   ```

## Maintenance
- Extend core logic by adding modules to `schedule/` or `allocation/`.
- Adapt to new scenarios by cloning `hcd-daycut` and modifying configuration/rules.
- Update API specs in `API_Documentation.md` when modifying the API layer.

This repository serves as a production-ready solution for Huachang Kuda's warehouse scheduling needs, enabling efficient, scalable, and observable warehouse operations.
