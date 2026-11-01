import csv, sys
f = open(sys.argv[1])
r = csv.DictReader(f)
types = {}
for row in r:
    t = row['event_type']
    types[t] = types.get(t, 0) + 1
for k, v in sorted(types.items(), key=lambda x: -x[1]):
    print(f"{v:4d} {k}")
