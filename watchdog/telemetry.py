import psutil
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Telemetry")

def find_target_process(script_name="main:app"):
    """Find the uvicorn process running our FastAPI app."""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['cmdline'] and script_name in ' '.join(proc.info['cmdline']):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

def monitor_process(interval_sec=1):
    logger.info("Starting telemetry monitor...")
    target_proc = find_target_process()
    
    if not target_proc:
        logger.error("Target application not found! Is uvicorn running?")
        return

    logger.info(f"Found target process: PID {target_proc.pid}")

    while True:
        try:
            # Get CPU and Memory usage
            cpu_percent = target_proc.cpu_percent(interval=interval_sec)
            mem_info = target_proc.memory_info()
            mem_mb = mem_info.rss / (1024 * 1024) # Convert bytes to MB
            
            logger.info(f"PID: {target_proc.pid} | CPU: {cpu_percent}% | RAM: {mem_mb:.2f} MB")
            
        except psutil.NoSuchProcess:
            logger.error("Process crashed or was terminated!")
            break

if __name__ == "__main__":
    monitor_process()
