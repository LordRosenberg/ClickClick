---
name: chrome-grid-maze
description: 在浏览器网格迷宫中识别当前位置、目标和障碍，分析最短可达路径与动作预算，使用方向按钮逐步到达目标。
version: 0.2.0
app: com.android.chrome
interface_scope: app
kind: workflow
capability: grid_maze_navigation
tags: [browser, maze, grid, navigation]
source: authored
---

# Navigate a grid maze

## Procedure

Read the current marker, destination, cell boundaries and blocked cells from pixels. The accessibility tree may expose only direction buttons and omit the maze contents; this does not make the screenshot's grid unavailable.

Analyze the shortest path before choosing the next direction. For a grid where each direction-button activation moves one cell at equal cost, use breadth-first reasoning: expand reachable cells by distance from the current marker, respecting walls and boundaries, then reconstruct a route when the goal is first reached. Check every adjacent pair and count moves. A valid route may temporarily move away from the destination, but a visually plausible detour is not evidence of necessity. Use the actual current position after each move.

Compare the shortest supported move count with the remaining device-action budget, including any required submission. Observation/model calls have separate budgets; do not add a direction tap merely to verify arrival. If a recorded route exceeds the budget, recheck the grid and shorter alternatives before reporting infeasibility. Keep observed cells separate from candidate routes in notes, and claim a minimum or unique route only when the analysis establishes it. Use concise route/count notes; no exhaustive search transcript is needed.

When a direction produces no marker movement, compare the adjacent cell and outer boundary. If blocked, choose a different traversable neighbor. Repeated taps or waiting cannot cross a wall. A button's focus changing is not marker movement. Submit for review only when the goal cell or the page's success state is visibly reached.

Routes, grid size, colors and marker locations must come from current evidence. Never reuse a previously solved maze's move sequence.

## Verification

The marker visibly reaches the goal cell or the page displays its success state. Button focus and dispatched movement intents do not establish completion.
