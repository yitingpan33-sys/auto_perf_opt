# 性能瓶颈代码分析

基于 perf 热点数据，Top 10 CPU 热点函数及对应瓶颈如下：

---

## 瓶颈1：FrameDispatcher::executePosition — 错误的并行粒度（CPU 9.38%）

**文件**: `src/frame_dispatcher.cpp`

**问题**: Position 阶段外层串行遍历粒子，内层对每个粒子的 100 次微步做并行。这导致：
- OpenMP 线程在每次内层循环都要同步（fork/join 开销 × 粒子数）
- 粒子间完全串行，无法利用多核
- 将 dt 拆成 100 个微步无物理意义，徒增开销

```cpp
// 瓶颈代码（当前）
if (SolverOpenMP::enable_wrong_parallel_granularity)
{
    for (int i = 0; i < static_cast<int>(particles.size()); ++i)
    {
#pragma omp parallel for num_threads(num_threads)  // 每个粒子都 fork/join 一次！
        for (int j = 0; j < 100; ++j)             // 100 次无意义微步
        {
            PositionPipeline::execute(particles[i], dt / 100.0);
        }
    }
}
```

**优化方向**: 应在粒子维度并行，每个粒子只执行一次：

```cpp
#pragma omp parallel for num_threads(num_threads)
for (int i = 0; i < static_cast<int>(particles.size()); ++i)
{
    auto& p = particles[i];
    if (!p.is_active) continue;
    PositionPipeline::execute(p, dt);
}
```

---

## 瓶颈2：Physics::check_collisions — O(n²) 碰撞检测（CPU 6.91%）

**文件**: `src/physics.cpp`

**问题**: 暴力双重循环检测碰撞，时间复杂度 O(n²)，粒子数多时成为主要瓶颈。

```cpp
void Physics::check_collisions(ParticleSystem& system, double /*dt*/) {
    auto& particles = system.get_particles();
    int n = particles.size();
    for (int i = 0; i < n; ++i) {
        for (int j = i + 1; j < n; ++j) {  // O(n²)
            // ...距离计算 + 碰撞响应...
        }
    }
}
```

**优化方向**: 使用空间划分（网格/八叉树）将复杂度降至 O(n·k)，k 为近邻数。

---

## 瓶颈3：Physics::apply_gravity — 故意的分支预测失败（CPU 占比较高，包含在 ForcePipeline 调用链中）

**文件**: `src/physics.cpp`

**问题**: `enable_branch_misprediction=true` 时，对每个粒子做 `id % 2`、`mass > 0.5`、`radius > 0.3` 三个难以预测的分支，导致流水线频繁冲刷。

```cpp
if (enable_branch_misprediction) {
    if (p.id % 2 == 0) {       // 50% 概率，分支预测失败率高
        p.vy -= g * dt;
    } else {
        p.vy -= g * dt * 0.9;
    }
    if (p.mass > 0.5) {        // 同上
        p.vx *= 0.999;
    } else {
        p.vx *= 0.998;
    }
    // ...
}
```

**优化方向**: 用条件传送（cmov）替代分支，或直接用数学表达式消除分支。

---

## 瓶颈4：大量无用的 volatile 计算 — 函数调用层级膨胀

多个 Pipeline 中存在用 `volatile` 写入然后 `(void)cast` 的伪操作，这些代码：
- 阻止编译器优化（`volatile` 强制读写内存）
- 增加函数调用层级，降低指令缓存命中率
- 不产生任何有用结果

涉及文件：

| 文件 | 函数 | 无用 volatile |
|------|------|--------------|
| `position_pipeline.cpp` | `PositionUpdater::updateHistory` | `volatile double history = p.x*0.1 + p.y*0.2 + p.z*0.3` |
| `position_pipeline.cpp` | `PositionPipeline::preUpdate` | `volatile double tmp = p.mass * p.radius` |
| `position_pipeline.cpp` | `PositionPipeline::postUpdate` | `volatile double checksum = p.x+p.y+p.z+p.vx+p.vy+p.vz` |
| `force_pipeline.cpp` | `GravityProcessor::computeAcceleration` | `volatile double estimate = p.mass * 9.81 * dt` |
| `force_pipeline.cpp` | `DragProcessor::apply` | `volatile double drag = ...` |
| `force_pipeline.cpp` | `MotionStatistics::sampleVelocity` | `volatile double speed = sqrt(...)` |
| `force_pipeline.cpp` | `MotionStatistics::sampleMass` | `volatile double m = p.mass` |
| `force_pipeline.cpp` | `MotionStatistics::sampleRadius` | `volatile double r = p.radius` |
| `force_pipeline.cpp` | `ForcePipeline::preProcess` | `volatile double value = p.x+p.y+p.z` |
| `force_pipeline.cpp` | `ForcePipeline::postProcess` | `volatile double checksum = p.vx+p.vy+p.vz` |
| `collision_pipeline.cpp` | `CollisionPreprocess::sampleActiveParticles` | `volatile int activeCount = 0` |
| `collision_pipeline.cpp` | `CollisionPreprocess::warmupCache` | `volatile double dummy = 0.0` |
| `collision_pipeline.cpp` | `BroadPhaseProfiler` 多个函数 | `volatile double density/centerX` |
| `collision_pipeline.cpp` | `CollisionPostprocess::updateVelocityStatistics` | `volatile double avgSpeed = 0.0` |
| `collision_pipeline.cpp` | `CollisionPostprocess::updateEnergyHint` | `volatile double hint = 0.0` |
| `collision_pipeline.cpp` | `CollisionPipeline::preExecute/postExecute` | `volatile count/checksum` |
| `simulation_engine.cpp` | `beforeFrame/afterFrame` | `volatile particleCount/checksum` |
| `energy_pipeline.cpp` | `preCompute/postCompute` | `volatile count/checksum` |
| `diagnostics.cpp` | `updateHistogram/updateCounter` | `volatile bucket/c` |

---

## 瓶颈5：CollisionPipeline 冗余预处理/后处理 — 多次遍历粒子数组

**文件**: `src/collision_pipeline.cpp`

**问题**: 碰撞管道在真正碰撞检测前后各遍历粒子数组多次（sampleActiveParticles、warmupCache、estimateDensity、estimateSpatialDistribution、updateVelocityStatistics、updateEnergyHint），每次遍历都是独立的循环，缓存利用率差。

**优化方向**: 合并遍历，一次循环完成所有统计。

---

## 瓶颈6：PositionPipeline 过度拆分 — 微操作各占一个函数

**文件**: `src/position_pipeline.cpp`

**问题**: updateX/updateY/updateZ 各自一个函数调用，preUpdate/postUpdate 是 volatile 伪操作，CoordinateNormalizer 在 1e-12 阈值下几乎不可能触发。每次 execute 有 ~8 层函数调用，增加开销。

**优化方向**: 内联合并，删除无用操作。