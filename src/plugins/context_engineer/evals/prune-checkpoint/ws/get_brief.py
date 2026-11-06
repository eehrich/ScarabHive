"""Prints the brief of one task, once: the script deletes itself, so the brief exists only in the conversation."""
import os
import sys

BRIEFS = {
    "ledger": [
        "The report is for the auditor Ilse Vandermolen; address her by name.",
        "The first line of your final answer must be the reference code TK-8152.",
        "State the batch number 4417.",
        "Give the total with the unit kg.",
    ],
    "harbour": [
        "The shipment goes to the depot in Lerwick; name the depot.",
        "The contact person is Dagny Fjellstad; name her.",
        "The delivery deadline is 14 March; state it.",
        "End your final answer with the line: Signed: QA-7",
    ],
    "bluefinch": [
        "The project codename is Larkspur; name it.",
        "The budget cap is 2900 euros; state it.",
        "The reviewer is Tomasz Wierzba; name him.",
        "Write the total with a hash sign in front, like #123.",
    ],
    "orchard": [
        "The report goes out in the name of the backup site Rjukan; name it.",
        "The approval threshold is 63 percent; state it.",
        "The colour code of this run is teal; state it.",
        "Put the total in square brackets, like [123].",
    ],
    "quarry": [
        "The site manager is Femi Oyelaran; name him.",
        "The permit number is 5530; state it.",
        "The material is basalt; name it.",
        "Begin your final answer with the word ATTENTION.",
    ],
    "meridian": [
        "The vessel is the Kestrel Maru; name it.",
        "The berth is B-31; state it.",
        "The inspector is Annelie Sorvik; name her.",
        "End your final answer with the word OVER on a line of its own.",
    ],
}

if __name__ == "__main__":
    case = sys.argv[1] if len(sys.argv) > 1 else ""
    if case not in BRIEFS:
        sys.exit(f"usage: python get_brief.py <{'|'.join(BRIEFS)}>")
    print("BRIEF -- shown only once, it cannot be fetched again. It applies to your final answer:")
    for line in BRIEFS[case]:
        print(f"- {line}")
    os.remove(__file__)
