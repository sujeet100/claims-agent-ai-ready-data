"""Record the agent at every stage, so the demo can be replayed without a network.

    python -m claims.record                  # all stages, claims officer
    python -m claims.record F E              # just these stages
"""

import sys

from claims import agent, plane


def main(stages: list[str]):
    for stage in stages or plane.STAGES:
        r = agent.run(stage)
        marks = " ".join(("✓" if row["pass"] else "✗") for row in r["scorecard"])
        print(f"{stage:<7} {r['seconds']:>5}s  {len(r['trace']):>2} calls  {marks}  {r['assessment']['decision']}")


if __name__ == "__main__":
    main(sys.argv[1:])
