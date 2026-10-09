"""Public image task facade; all execution goes through the durable scheduler."""
from services.generation_protocol import DurableImageTasks

image_task_service = DurableImageTasks()
