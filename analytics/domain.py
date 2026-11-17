"""车队车辆与运营线路的当前分配。"""
class FleetRoster:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS vehicles(vehicle TEXT PRIMARY KEY, power TEXT, route TEXT)")

    def assign(self, vehicle, power, route):
        if power not in {"electric", "diesel"}: raise ValueError("动力类型无效")
        self.connection.execute("INSERT OR REPLACE INTO vehicles VALUES(?,?,?)", (vehicle,power,route))

    def route(self, vehicle):
        row = self.connection.execute("SELECT route FROM vehicles WHERE vehicle=?", (vehicle,)).fetchone()
        return row[0] if row else None
