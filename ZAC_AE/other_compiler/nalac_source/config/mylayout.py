class Zone:
    def __init__(self, name, x, y, dx, dy, cols, rows):
        self.name = name
        self.x = x
        self.y = y
        self.dx = dx
        self.dy = dy
        self.cols = cols
        self.rows = rows
LAYOUT_FILE = "mylayout.csv"
zones = [
    Zone("entangling", 35, 307, 10, 12, 20, 7),
    Zone("storage", 0, 0, 3, 3, 100, 100),
    Zone("readout", 0, 390, 5, 5, 1, 1)
]
with open(LAYOUT_FILE, "w") as f:
    f.write("x,y\n")
    for zone in zones:
        for i in range(zone.rows):
            y = zone.y + zone.dy * i
            for j in range(zone.cols):
                x = zone.x + zone.dx * j
                f.write(f"{x},{y}\n")