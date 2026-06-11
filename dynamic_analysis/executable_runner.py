import subprocess
import os
import time
from common.logger import logger

class ExecutableRunner:
    def __init__(self, project_config):
        self.config = project_config
        self.executable_path = project_config.executable.path
        self.args = project_config.executable.args
        self.run_duration = project_config.executable.run_duration

    def run_and_profile(self, perf_output_dir):
        logger.info(f"准备运行可执行文件: {self.executable_path}")
        if not os.path.exists(self.executable_path):
            logger.error(f"可执行文件不存在: {self.executable_path}")
            raise FileNotFoundError(f"可执行文件不存在: {self.executable_path}")
        if not os.access(self.executable_path, os.X_OK):
            logger.error(f"可执行文件没有执行权限: {self.executable_path}")
            raise PermissionError(f"可执行文件没有执行权限: {self.executable_path}")

        if not os.path.exists(perf_output_dir):
            os.makedirs(perf_output_dir)
        perf_data_path = os.path.join(perf_output_dir, "perf_data")
        perf_events = (
            "cycles,instructions,"
            "cache-references,cache-misses,"
            "branch-instructions,branch-misses,"
            "L1-dcache-loads,"
            "LLC-loads,LLC-load-misses"
        )
        perf_cmd = [
           "perf", "record",
           "-g", "-F", "99",
           "-e", perf_events,
           "-o", perf_data_path,
           "--", self.executable_path
       ] + self.args
        if self.run_duration:
            logger.info(f"启动性能分析，将运行 {self.run_duration} 秒")
        else:
            logger.info("启动性能分析，将等待程序自然运行结束")
        logger.info(f"执行命令: {' '.join(perf_cmd)}")

        try:
            process = subprocess.Popen(perf_cmd, cwd = os.path.join(self.config.project_path, "build"))
            if self.run_duration:
                start_time = time.time()
                while time.time() - start_time < self.run_duration:
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                if process.poll() is None:
                    logger.info(f"运行时间已到，终止进程")
                    process.terminate()
                    process.wait(timeout=5)
            else:
                logger.info("程序正在运行，请手动关闭程序或等待它自然结束...")
                process.wait()
            if process.returncode not in [0, -15]:
                logger.error(f"进程异常退出，返回码: {process.returncode}")
                raise RuntimeError(f"可执行文件运行失败，返回码: {process.returncode}")
            logger.info("程序运行完成，perf数据采集成功")

            perf_script_path = os.path.join(perf_output_dir, "perf.script")
            with open(perf_script_path, "w") as f:
                subprocess.run(
                    ["perf", "script", "-i", perf_data_path],
                    stdout=f, check=True
                )

            # ── 运行 perf stat 采集程序级聚合指标 ──
            perf_stat_path = os.path.join(perf_output_dir, "perf_stat.txt")
            stat_events = (
                "cycles,instructions,"
                "cache-references,cache-misses,"
                "branch-instructions,branch-misses,"
                "L1-dcache-loads,"
                "LLC-loads,LLC-load-misses"
            )
            stat_cmd = [
                "perf", "stat",
                "-e", stat_events,
                "-o", perf_stat_path,
                "--", self.executable_path
            ] + self.args
            logger.info(f"运行 perf stat 采集聚合指标...")
            try:
                subprocess.run(
                    stat_cmd,
                    cwd=os.path.join(self.config.project_path, "build"),
                    timeout=self.run_duration + 30 if self.run_duration else None,
                    check=False  # perf stat 可能非零退出，数据仍有效
                )
                if os.path.exists(perf_stat_path):
                    logger.info(f"perf stat 数据已保存: {perf_stat_path}")
            except subprocess.TimeoutExpired:
                logger.warning("perf stat 超时，跳过聚合指标采集")
            except Exception as e:
                logger.warning(f"perf stat 运行失败: {e}")

            return {"perf_data": perf_data_path,
                    "perf_script": perf_script_path,
                    "perf_stat": perf_stat_path if os.path.exists(perf_stat_path) else None}
        except subprocess.TimeoutExpired:
            logger.error("进程终止超时，强制杀死")
            process.kill()
            raise
        except KeyboardInterrupt:
            logger.info("用户手动中断，正在停止性能分析...")
            process.terminate()
            process.wait()
            raise
        except Exception as e:
            logger.error(f"运行可执行文件失败: {e}")
            raise