# 测试用替身：只提供 speedup 合并文字框时用到的 Box，构造参数与 ok/feature/Box.py 一致
class Box:
    def __init__(self, x, y, width=0, height=0, confidence=1.0, name=None, to_x=-1, to_y=-1):
        self.x, self.y = int(round(x)), int(round(y))
        self.width = int(round(to_x - x)) if to_x >= 0 else int(round(width))
        self.height = int(round(to_y - y)) if to_y >= 0 else int(round(height))
        self.confidence, self.name = confidence, name

    def area(self):
        return self.width * self.height
