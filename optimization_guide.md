# 性能优化指南 — particle_simulation

> 基于 perf 采样数据 (484,924 samples, 10000 particles, 1000 iterations, 8 threads)

---

## 总览：按优先级排序的优化项

| 优先级 | 优化项 | CPU影响 | 难度 | 类型 |
|--------|--------|---------|------|------|
| **P0** | 删除所有 `volatile` 无用计算 | **-25% CPU** | 极低 | 删除死代码 |
| **P0** | 关闭错误并行粒度 | **-3% CPU + 消除66%分支错误** | 极低 | 改一行 |
| **P1** | 关闭 `apply_gravity` 分支预测陷阱 | **-2% CPU** | 极低 | 改一行 |
| **P1** | 碰撞检测加空间网格 | **-20% CPU, -90% CacheMiss** | 中 | 算法优化 |
| **P2** | 内联微函数, 展平调用层级 | **-10% CPU** | 低 | 函数合并 |
| **P3** | Particle 结构体重排 (AoS→SoA) | **-8% CacheMiss** | 中 | 数据布局 |

---

## P0: 删除所有 `volatile` 无用计算（影响 ~25% CPU）

### 问题

以下 `volatile` 代码在每个粒子每帧都执行，但结果被 `(void)` 丢弃，纯粹是性能陷阱：

```cpp
// ❌ 这些 volatile 操作阻止编译器优化，强制内存读写
volatile double tmp = p.mass * p.radius;
(void)tmp;
```

### 需要修改的文件和函数

#### 文件 1: `src/position_pipeline.cpp`

**函数 `PositionUpdater::updateHistory`**（当前 1.65% CPU）：
```cpp
// ❌ 当前代码
void PositionUpdater::updateHistory(Particle& p)
{
    volatile double history = p.x * 0.1 + p.y * 0.2 + p.z * 0.3;
    (void)history;
}

// ✅ 优化后：直接删除此函数
```

**函数 `PositionPipeline::preUpdate`**（当前 6.65% CPU）：
```cpp
// ❌ 当前代码
void PositionPipeline::preUpdate(Particle& p)
{
    volatile double tmp = p.mass * p.radius;
    (void)tmp;
}

// ✅ 优化后：直接删除此函数
```

**函数 `PositionPipeline::postUpdate`**（当前 13.38% CPU）：
```cpp
// ❌ 当前代码
void PositionPipeline::postUpdate(Particle& p)
{
    volatile double checksum = p.x + p.y + p.z + p.vx + p.vy + p.vz;
    (void)checksum;
}

// ✅ 优化后：直接删除此函数
```

**优化后的 `PositionPipeline::execute`**：
```cpp
void PositionPipeline::execute(Particle& p, double dt)
{
    // preUpdate: 已删除 (无用 volatile)
    // updateHistory: 已删除 (无用 volatile)
    // postUpdate: 已删除 (无用 volatile)

    // 内联 updateX/Y/Z，避免 4 次函数调用
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    p.z += p.vz * dt;

    // 边界处理保留（有实际逻辑）
    BoundaryProcessor::process(p);
    CoordinateNormalizer::normalize(p);
}
```

#### 文件 2: `src/force_pipeline.cpp`

**函数 `GravityProcessor::computeAcceleration`**：
```cpp
// ❌ 删除
void GravityProcessor::computeAcceleration(Particle& p, double dt)
{
    volatile double estimate = p.mass * 9.81 * dt;
    (void)estimate;
}
```

**函数 `DragProcessor::apply` — 删除 volatile drag 计算**：
```cpp
void DragProcessor::apply(Particle& p)
{
    // ❌ 删除 volatile 部分
    // volatile double drag = coefficient * density * ...;
    // (void)drag;

    // ✅ 如果确实不需要阻力计算，直接返回
}
```

**函数 `MotionStatistics::sampleVelocity/sampleMass/sampleRadius`**：
```cpp
// ❌ 全部删除 — 3 个函数各含一句 volatile，合计浪费 CPU
void MotionStatistics::sampleVelocity(const Particle& p) { ... }  // 删
void MotionStatistics::sampleMass(const Particle& p)     { ... }  // 删
void MotionStatistics::sampleRadius(const Particle& p)   { ... }  // 删
```

**函数 `ForcePipeline::preProcess/postProcess`**：
```cpp
// ❌ 全部删除 — volatile 无用操作
void ForcePipeline::preProcess(Particle& p)  { ... }   // 删
void ForcePipeline::postProcess(Particle& p) { ... }   // 删
```

**优化后的 `ForcePipeline::execute`**：
```cpp
void ForcePipeline::execute(Particle& p, double dt)
{
    // preProcess: 删除
    Physics::apply_gravity(p, dt);    // 简化：原来经 GravityProcessor 两层转发
    // DragProcessor: 删除（内部无实际逻辑）
    VelocityLimiter::limit(p);        // 保留（有实际逻辑）
    // MotionStatistics: 删除
    // postProcess: 删除
}
```

#### 文件 3: `src/collision_pipeline.cpp`

```cpp
// ❌ 删除以下全部 volatile 操作：
// CollisionPreprocess::sampleActiveParticles 中的 volatile int
// CollisionPreprocess::warmupCache 中的 volatile double
// BroadPhaseProfiler::estimateDensity 中的 volatile double
// BroadPhaseProfiler::estimateSpatialDistribution 中的 volatile double
// CollisionPostprocess::updateVelocityStatistics 中的 volatile double
// CollisionPostprocess::updateEnergyHint 中的 volatile double
// CollisionPipeline::preExecute/postExecute 中的 volatile
```

#### 文件 4: `src/simulation_engine.cpp`

```cpp
// ❌ 删除 beforeFrame/afterFrame 中的 volatile 操作
```

#### 文件 5: `src/energy_pipeline.cpp`

```cpp
// ❌ 删除 preCompute/postCompute 中的 volatile 操作
```

---

## P0: 关闭错误并行粒度（影响 3.3% CPU, 消除 66% 分支错误）

### 文件: `src/solver_openmp.cpp` + `src/frame_dispatcher.cpp`

**根本原因**: `SolverOpenMP::enable_wrong_parallel_granularity = true` 触发了错误路径，每个粒子都做一次 OpenMP fork/join（10000 次！），且内部 100 个 dt/100 微步无物理意义。

**修改**：

```cpp
// src/solver_openmp.cpp
// ❌ 当前: 默认开启错误的并行粒度
bool SolverOpenMP::enable_wrong_parallel_granularity = true;

// ✅ 优化: 关闭，使用正确的粒子级并行
bool SolverOpenMP::enable_wrong_parallel_granularity = false;
```

打开正确路径后的代码（`frame_dispatcher.cpp` 已存在，只需切换 flag）：

```cpp
// ✅ 正确: 粒子级并行，每个粒子只执行一次
#pragma omp parallel for num_threads(num_threads)
for (int i = 0; i < static_cast<int>(particles.size()); ++i)
{
    auto& p = particles[i];
    if (!p.is_active) continue;
    PositionPipeline::execute(p, dt);
}
```

---

## P1: 关闭 `apply_gravity` 分支预测陷阱（影响 ~2% CPU）

### 文件: `src/physics.cpp`

**根本原因**: `enable_branch_misprediction = true` 在 `apply_gravity` 中对每个粒子执行 3 个 50% 概率的 `if/else`，导致分支预测器失效。

**修改**：

```cpp
// ❌ 当前: 故意制造分支预测失败
bool Physics::enable_branch_misprediction = true;

// ✅ 优化: 使用无分支路径
bool Physics::enable_branch_misprediction = false;
```

无分支版本（已存在）：
```cpp
// ✅ 这个分支在 enable_branch_misprediction=false 时执行
p.vy -= g * dt;       // 无条件
p.vx *= 0.9985;       // 无条件
p.vz *= 0.9965;       // 无条件
```

---

## P1: 碰撞检测加空间网格（影响 ~20% CPU, ~90% Cache Miss 改善）

### 文件: `src/physics.cpp` — `Physics::check_collisions`

**当前问题**:
- O(n²) 双重循环：10000 粒子 → 5000万次距离计算/帧
- 93.25% Cache Miss 率：内层循环跳跃访问 `particles[j]`
- 74.48% 分支预测失败率：`is_active` 检查 + `dvn > 0` 检查不可预测

**优化方案：均匀网格空间划分**：

```cpp
// ✅ 优化后的 check_collisions
void Physics::check_collisions(ParticleSystem& system, double /*dt*/) {
    auto& particles = system.get_particles();
    int n = particles.size();

    // 1. 构建空间网格
    constexpr double CELL_SIZE = 2.0;   // 根据粒子半径调整
    constexpr double GRID_MIN = -10.0;  // 与 kBoundary 一致
    constexpr int GRID_DIM = 10;        // (20/2) = 10 cells per axis

    struct Cell { std::vector<int> ids; };
    std::vector<Cell> grid(GRID_DIM * GRID_DIM * GRID_DIM);

    // 2. 将粒子分配到网格（O(n), 缓存友好）
    for (int i = 0; i < n; ++i) {
        const auto& p = particles[i];
        if (!p.is_active) continue;
        int cx = static_cast<int>((p.x - GRID_MIN) / CELL_SIZE);
        int cy = static_cast<int>((p.y - GRID_MIN) / CELL_SIZE);
        int cz = static_cast<int>((p.z - GRID_MIN) / CELL_SIZE);
        if (cx < 0 || cx >= GRID_DIM || cy < 0 || cy >= GRID_DIM || cz < 0 || cz >= GRID_DIM)
            continue;
        grid[cx + cy * GRID_DIM + cz * GRID_DIM * GRID_DIM].ids.push_back(i);
    }

    // 3. 只在相邻网格中检测碰撞（O(n·k), k ≈ 近邻数）
    for (int cx = 0; cx < GRID_DIM; ++cx) {
        for (int cy = 0; cy < GRID_DIM; ++cy) {
            for (int cz = 0; cz < GRID_DIM; ++cz) {
                auto& cell = grid[cx + cy * GRID_DIM + cz * GRID_DIM * GRID_DIM];
                if (cell.ids.size() < 2) continue;

                // 检测当前 cell 内的碰撞 + 相邻 13 个 cell (避免重复)
                for (int dcx = 0; dcx <= 1; ++dcx) {
                    for (int dcy = -1; dcy <= 1; ++dcy) {
                        for (int dcz = -1; dcz <= 1; ++dcz) {
                            if (dcx == 0 && dcy < 0) continue;
                            if (dcx == 0 && dcy == 0 && dcz <= 0) continue;

                            int nx = cx + dcx, ny = cy + dcy, nz = cz + dcz;
                            if (nx < 0 || nx >= GRID_DIM || ny < 0 ||
                                ny >= GRID_DIM || nz < 0 || nz >= GRID_DIM)
                                continue;

                            auto& neighbor = grid[nx + ny * GRID_DIM + nz * GRID_DIM * GRID_DIM];
                            checkCellPairs(particles, cell.ids, neighbor.ids);
                        }
                    }
                }

                // 检测同一 cell 内的碰撞
                checkCellPairs(particles, cell.ids, cell.ids);
            }
        }
    }
}

// 辅助函数：检测两个 cell 的粒子对
static void checkCellPairs(std::vector<Particle>& particles,
                           const std::vector<int>& ids1,
                           const std::vector<int>& ids2) {
    bool same_cell = (&ids1 == &ids2);
    for (size_t a = 0; a < ids1.size(); ++a) {
        int i = ids1[a];
        auto& p1 = particles[i];
        size_t start = same_cell ? a + 1 : 0;
        for (size_t b = start; b < ids2.size(); ++b) {
            int j = ids2[b];
            auto& p2 = particles[j];

            double dx = p2.x - p1.x;
            double dy = p2.y - p1.y;
            double dz = p2.z - p1.z;
            double dist_sq = dx*dx + dy*dy + dz*dz;
            double min_dist = p1.radius + p2.radius;

            if (dist_sq < min_dist * min_dist) {
                double dist = sqrt(dist_sq);
                double nx = dx / dist, ny = dy / dist, nz = dz / dist;
                double dvn = (p2.vx - p1.vx)*nx + (p2.vy - p1.vy)*ny + (p2.vz - p1.vz)*nz;
                if (dvn > 0) continue;

                double impulse = (2.0 * dvn) / (p1.mass + p2.mass);
                p1.vx += impulse * p2.mass * nx;
                p1.vy += impulse * p2.mass * ny;
                p1.vz += impulse * p2.mass * nz;
                p2.vx -= impulse * p1.mass * nx;
                p2.vy -= impulse * p1.mass * ny;
                p2.vz -= impulse * p1.mass * nz;
            }
        }
    }
}
```

**效果**: O(n²) → O(n·k), Cache Miss 从 93% → ~5%

---

## P2: 内联微函数，展平调用层级（影响 ~10% CPU）

### 文件: `src/position_pipeline.cpp`

**当前问题**: 每个粒子每帧调用链深度为 5-8 层：
```
FrameDispatcher → PositionPipeline::execute → preUpdate (volatile)
                                            → PositionUpdater::update → updateX (一行)
                                                                      → updateY (一行)
                                                                      → updateZ (一行)
                                                                      → updateHistory (volatile)
                                            → BoundaryProcessor::process → processX/Y/Z
                                            → CoordinateNormalizer::normalize → normalizeX/Y/Z
                                            → postUpdate (volatile)
```

大量函数只做一件极简单的事（如 `p.x += p.vx * dt`），但编译器可能不内联跨翻译单元的函数。

**修改**：合并 `PositionUpdater` 和 `PositionPipeline`：

```cpp
// ✅ 优化后的 PositionPipeline::execute
void PositionPipeline::execute(Particle& p, double dt)
{
    // 内联位置更新（原 updateX/Y/Z）
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    p.z += p.vz * dt;

    // 边界处理
    constexpr double kB = 10.0;
    if (p.x < -kB || p.x > kB) { p.vx *= -1.0; p.x = utils::clamp(p.x, -kB, kB); }
    if (p.y < -kB || p.y > kB) { p.vy *= -1.0; p.y = utils::clamp(p.y, -kB, kB); }
    if (p.z < -kB || p.z > kB) { p.vz *= -1.0; p.z = utils::clamp(p.z, -kB, kB); }

    // 规范化（几乎永不触发，可删）
    if (std::fabs(p.x) < 1e-12) p.x = 0.0;
    if (std::fabs(p.y) < 1e-12) p.y = 0.0;
    if (std::fabs(p.z) < 1e-12) p.z = 0.0;
}
```

从 8 个函数调用合并为 1 个，消除所有函数调用开销。

---

## P3: Particle 结构体重排（改善缓存局部性）

### 文件: `include/particle.h`

**当前问题**: `sizeof(Particle) = 72 bytes`，但大多数阶段只访问位置和速度（48 bytes），质量、半径、id、is_active 浪费缓存行空间。

```cpp
// ❌ 当前: AoS (Array of Structures) — 72 bytes/particle
struct Particle {
    double x, y, z;       // 24 bytes
    double vx, vy, vz;    // 24 bytes
    double mass;          // 8 bytes
    double radius;        // 8 bytes
    int id;               // 4 bytes
    bool is_active;       // 1 byte + 3 padding
};  // total: 72 bytes — 跨 2 个缓存行

// ✅ 优化: 分离冷热数据
struct ParticleHot {
    double x, y, z;       // 24 bytes — 热: 位置更新每帧读
    double vx, vy, vz;    // 24 bytes — 热: 速度每帧读写
};  // 48 bytes — 刚好 3/4 缓存行

struct ParticleCold {
    double mass;          // 冷: 仅在碰撞时读
    double radius;        // 冷: 仅在碰撞时读
    int id;               // 冷: 仅在 apply_gravity 时读
    bool is_active;       // 冷: 极少变化
};

struct Particle {
    ParticleHot hot;
    ParticleCold cold;
};
```

这个改动涉及面广，建议在其他优化实施后再评估。

---

## 完整优化清单

按实施顺序排列：

### 第一步：改两行配置（立即生效，零风险）

1. `src/solver_openmp.cpp:11` → `enable_wrong_parallel_granularity = false;`
2. `src/physics.cpp:5` → `enable_branch_misprediction = false;`

### 第二步：删除死代码（立即生效，零风险）

删除以下文件中的全部 `volatile` 操作和 `(void)` 丢弃：
- `src/position_pipeline.cpp`: `updateHistory`, `preUpdate`, `postUpdate`
- `src/force_pipeline.cpp`: `computeAcceleration`, `DragProcessor::apply` 中的 volatile, `MotionStatistics` 全部, `preProcess`, `postProcess`
- `src/collision_pipeline.cpp`: 全部 `volatile` 变量
- `src/simulation_engine.cpp`: `beforeFrame`, `afterFrame`
- `src/energy_pipeline.cpp`: `preCompute`, `postCompute`

### 第三步：内联合并（需要测试）

将 `PositionPipeline::execute` 内联所有子函数调用。

### 第四步：空间哈希碰撞检测（需要验证正确性）

用网格划分替换 `Physics::check_collisions` 的 O(n²) 循环。

---

## 预期性能提升

| 步骤 | 预计 CPU 降低 | 累积 CPU |
|------|-------------|----------|
| 当前基线 | — | 303s/1000iter |
| 改两行配置 | -5% | 288s |
| 删除 volatile 死代码 | -25% | 227s |
| 内联合并 | -8% | 209s |
| 空间哈希 | -18% | 172s |
| **总计** | **-43%** | **~172s** |

即 303 秒 → 172 秒，约 **1.76× 加速**。
