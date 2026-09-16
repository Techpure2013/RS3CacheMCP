"""
Work out which area every loc belongs to, and write it into the database as `loc_section`.

The rebuild does this itself (rs3mcp/areas.py has the rules). This redoes it on the installed
database, for trying a change to those rules without a whole rebuild.

  python rs3-areas.py
"""

import collections
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rs3mcp import areas, common


def main():
    db = sqlite3.connect(common.DB)
    zones = [d for (d,) in db.execute("select data from config where type='mapzones' and data is not null")]
    objects = list(db.execute("select id, name from config where type='objects'"))
    rows = areas.sections(zones, objects)

    db.execute("delete from loc_section")
    db.executemany("insert or replace into loc_section values (?,?,?)", rows)
    db.commit()

    sizes = collections.Counter(r[1] for r in rows)
    placed = sum(1 for r in rows if r[2])
    print("  %s locs placed, %s never placed" % (format(placed, ","), format(len(rows) - placed, ",")))
    print("  %s locs -> %d sections" % (format(len(rows), ","), len(sizes)))
    print("  biggest sections:")
    for n, c in sizes.most_common(10):
        print("     %-34s %d" % (n, c))


if __name__ == "__main__":
    main()
