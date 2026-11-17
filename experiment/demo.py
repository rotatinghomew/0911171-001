from .events import Conversion, EventLedger
from .cohorts import Cohort, Enrollment


def main() -> None:
    ledger = EventLedger()
    ledger.record(Conversion("evt-1", "reader-1", 12))
    cohort = Cohort("headline", ("control", "new"))
    for subject in ["reader-1", "reader-2", "reader-3"]:
        cohort.enroll(Enrollment(subject, 10, "regular"))
    print(cohort.observed_report(ledger, 20, "regular"))


if __name__ == "__main__":
    main()
