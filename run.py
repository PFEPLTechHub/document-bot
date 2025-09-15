import os
import sys
import time
import subprocess
import signal
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class BotReloader(FileSystemEventHandler):
    def __init__(self):
        self.process = None
        self.start_bot()

    def start_bot(self):
        if self.process:
            self.process.terminate()
            self.process.wait()
        
        print("Starting bot...")
        self.process = subprocess.Popen([sys.executable, "app.py"])

    def on_modified(self, event):
        if event.src_path.endswith('.py'):
            print(f"\nChanges detected in {event.src_path}")
            print("Reloading bot...")
            self.start_bot()

def main():
    reloader = BotReloader()
    observer = Observer()
    observer.schedule(reloader, path='.', recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        if reloader.process:
            reloader.process.terminate()
    observer.join()

if __name__ == "__main__":
    main() 