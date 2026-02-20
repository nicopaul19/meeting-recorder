from __future__ import annotations
import EventKit
import sys
import os

print(f"Python ver: {sys.version}")
print(sys.executable)
status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
print(f"EventKit Auth Status: {status}")
