# README.md
# Distribution Optimization for Manufacturing Scheduling
## Overview
This open-source repository provides a complete numerical simulation framework for production capacity allocation and time-series slicing optimization, targeting vehicle body manufacturing scheduling scenarios. It delivers implementable mathematical scheduling algorithms and end-to-end simulation pipelines to balance workshop load and mitigate production line congestion.

All core logic, simulation scripts and experimental analysis code are self-developed and fully open-sourced for industrial scheduling research and engineering reproduction.

## Core Modules
1. **hcd-carbody: Vehicle Body Capacity Allocation**
Establishes quantitative load balancing models for multi-station production lines. It computes reasonable capacity distribution constraints and realizes global workload equilibrium across manufacturing stations via numerical iteration.

2. **hcd-daycut: Daily Production Time Series Cutting**
Proposes an optimized time-slice segmentation algorithm for daily production tasks. The module splits large-scale manufacturing orders into reasonable time windows, effectively easing process bottlenecks and lifting overall production throughput.

## Key Features
- Self-designed scheduling optimization algorithms tailored to actual vehicle manufacturing scenarios
- End-to-end Python simulation pipeline with configurable production parameters
- Built-in data statistics & result visualization modules for scheduling effect evaluation
- Well-structured, decoupled code architecture easy for secondary development and algorithm iteration
- Complete Jupyter notebooks for algorithm verification and experimental reproduction

## Environment
```
Python >= 3.8
NumPy
Pandas
Matplotlib
```

## Quick Start
1. Clone this repository
```bash
git clone https://github.com/SCIENCE-FRESHMEN/distribution.git
cd distribution
```
2. Install dependencies
```bash
pip install -r requirements.txt
```
3. Run simulation cases
```bash
python run_simulation.py
```

## Application Scenarios
- Automobile workshop production scheduling
- Multi-station manufacturing capacity balancing
- Batch order time-window decomposition & optimization
- Academic research on discrete manufacturing scheduling algorithms

## License
MIT License
