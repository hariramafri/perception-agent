import time
import datetime
import traceback
import os
from contextlib import contextmanager

class TelemetryAgent:
    def __init__(self, output_dir="output"):
        self.output_dir = output_dir
        self.last_successful_step = "Initialization"
        self.driver = None
        os.makedirs(self.output_dir, exist_ok=True)
        
    def set_driver(self, driver):
        self.driver = driver

    def log(self, level, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{level}] {timestamp} - {message}")

    @contextmanager
    def track_action(self, action_name, element_state_callback=None):
        start_time = time.time()
        self.log("INFO", f"Initiating {action_name}...")
        try:
            yield
            duration = time.time() - start_time
            self.log("INFO", f"Completed {action_name} in {duration:.2f}s.")
            if duration > 2.0 and self.driver:
                # Limit length of filename
                safe_name = "".join([c if c.isalnum() else "_" for c in action_name])[:50]
                screenshot_path = os.path.join(self.output_dir, f"breadcrumb_{safe_name}.png")
                try:
                    if hasattr(self.driver, 'save_screenshot'):
                        self.driver.save_screenshot(screenshot_path)
                    elif hasattr(self.driver, 'screenshot'):
                        self.driver.screenshot(path=screenshot_path)
                    self.log("WARN", f"Action '{action_name}' took > 2s. Saved breadcrumb to {screenshot_path}")
                except Exception as e:
                    self.log("ERROR", f"Failed to save breadcrumb screenshot: {e}")
            self.last_successful_step = action_name
        except Exception as e:
            self.log("ERROR", f"Failed during: {action_name}")
            print("\n--- POST-MORTEM ---")
            print(f"Last successful step: {self.last_successful_step}")
            print(f"Error details: {str(e)}")
            if element_state_callback:
                try:
                    state = element_state_callback()
                    print(f"Element state: {state}")
                except Exception as state_err:
                    print(f"Could not retrieve element state: {state_err}")
            print("-------------------\n")
            raise e

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def async_track_action(self, action_name, element_state_callback=None):
        start_time = time.time()
        self.log("INFO", f"Initiating {action_name}...")
        try:
            yield
            duration = time.time() - start_time
            self.log("INFO", f"Completed {action_name} in {duration:.2f}s.")
            if duration > 2.0 and self.driver:
                safe_name = "".join([c if c.isalnum() else "_" for c in action_name])[:50]
                screenshot_path = os.path.join(self.output_dir, f"breadcrumb_{safe_name}.png")
                try:
                    if hasattr(self.driver, 'screenshot'):
                        await self.driver.screenshot(path=screenshot_path)
                    self.log("WARN", f"Action '{action_name}' took > 2s. Saved breadcrumb to {screenshot_path}")
                except Exception as e:
                    self.log("ERROR", f"Failed to save breadcrumb screenshot: {e}")
            self.last_successful_step = action_name
        except Exception as e:
            self.log("ERROR", f"Failed during: {action_name}")
            print("\n--- POST-MORTEM ---")
            print(f"Last successful step: {self.last_successful_step}")
            print(f"Error details: {str(e)}")
            if element_state_callback:
                try:
                    state = element_state_callback()
                    print(f"Element state: {state}")
                except Exception as state_err:
                    print(f"Could not retrieve element state: {state_err}")
            print("-------------------\n")
            raise e
