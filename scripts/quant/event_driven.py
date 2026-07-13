#!/usr/bin/env python3
"""
事件驱动框架
"""

from typing import Dict, Callable, List
from enum import Enum
from datetime import datetime

class EventType(Enum):
    MARKET_OPEN = "market_open"
    MARKET_CLOSE = "market_close"
    PRICE_UPDATE = "price_update"
    SIGNAL_GENERATED = "signal_generated"
    ORDER_FILLED = "order_filled"

class Event:
    def __init__(self, event_type: EventType, data: Dict):
        self.type = event_type
        self.data = data
        self.timestamp = datetime.now()

class EventBus:
    def __init__(self):
        self.handlers: Dict[EventType, List[Callable]] = {}
    
    def subscribe(self, event_type: EventType, handler: Callable):
        if event_type not in self.handlers:
            self.handlers[event_type] = []
        self.handlers[event_type].append(handler)
    
    def publish(self, event: Event):
        handlers = self.handlers.get(event.type, [])
        for handler in handlers:
            handler(event)


if __name__ == "__main__":
    print("=== 事件驱动测试 ===\n")
    
    bus = EventBus()
    
    def on_price_update(event):
        print(f"价格更新: {event.data}")
    
    bus.subscribe(EventType.PRICE_UPDATE, on_price_update)
    
    event = Event(EventType.PRICE_UPDATE, {'symbol': '513100', 'price': 2.20})
    bus.publish(event)
    
    print("\n✅ 事件驱动测试通过")
