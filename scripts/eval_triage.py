"""Measure triage quality on labelled tickets.

Run:  python -m scripts.eval_triage            (development set: what the rules were tuned on)
      python -m scripts.eval_triage --heldout  (held-out set: written before tuning, run once)

Reports category accuracy, sentiment accuracy, and priority accuracy two ways:
  * exact:      predicted level == labelled level
  * within one: off by at most one level (low < medium < high < urgent). Priority is partly
                subjective, so this is the fairer view of "reasonable".
Labels are what a sensible human agent would pick, not what the rules happen to output.
"""
import sys
from collections import Counter

from app.models import TicketPriority
from app.services.triage import RulesTriager, Triager

LEVEL = {p: i for i, p in enumerate([TicketPriority.low, TicketPriority.medium, TicketPriority.high, TicketPriority.urgent])}

# (subject, description, category, priority, sentiment)
DEV = [
    ("Cannot reset my password", "The reset email never arrives, I checked spam too.", "account", "high", "neutral"),
    ("Charged twice for my subscription", "I see two charges of the same amount on my card this month.", "billing", "high", "neutral"),
    ("Where is my order?", "Order placed 8 days ago and tracking has not updated.", "shipping", "medium", "neutral"),
    ("App crashes on startup", "Since the last update the app closes right after the splash screen.", "technical", "high", "neutral"),
    ("How do I change my email address?", "I want to move my account to my new email.", "account", "low", "neutral"),
    ("Refund still not received!!", "I was promised a refund two weeks ago and NOBODY has replied. This is unacceptable.", "billing", "high", "angry"),
    ("Invoice needs company GST number", "Please re-issue invoice #4821 with our GST details.", "billing", "medium", "neutral"),
    ("Wrong item delivered", "I ordered a blue jacket and received a black one.", "shipping", "medium", "neutral"),
    ("URGENT: account hacked", "Someone logged into my account and changed my password. I think I've been hacked, please help immediately.", "account", "urgent", "neutral"),
    ("Two-factor codes not working", "The 6 digit code is always rejected as invalid.", "account", "high", "neutral"),
    ("Need to cancel my plan", "Please cancel at the end of the billing period.", "billing", "medium", "neutral"),
    ("Delivery address change", "Can I change the address on an order that has not shipped?", "shipping", "low", "neutral"),
    ("Photo upload keeps failing", "Every time I try to upload a photo over 1 MB it spins forever. Third time this week.", "technical", "medium", "frustrated"),
    ("Love the new update", "Thanks, the new dashboard is great. Just wondering if dark mode is planned?", "product", "low", "positive"),
    ("I want to speak to a manager", "This is the worst service I've ever had. Nobody answers my emails and I'm sick of waiting.", "general", "high", "angry"),
    ("Order arrived damaged", "The package was crushed and the item inside is broken.", "shipping", "medium", "neutral"),
    ("Slow loading on website", "The pricing page takes forever to load on my phone.", "technical", "low", "neutral"),
    ("Charged after cancelling", "I cancelled my plan last month but you charged me again today. This is the second time.", "billing", "high", "frustrated"),
    ("Delete my account", "Please delete my account and all my data.", "account", "medium", "neutral"),
    ("Product size question", "Is the medium jacket true to size? I'm 5'10 and about 75 kg.", "product", "low", "neutral"),
]

HELDOUT = [
    ("Locked out", "My account got locked after too many tries and I have a presentation in an hour. Please unlock it ASAP.", "account", "urgent", "neutral"),
    ("Unrecognised transaction on my card", "I see a payment of 4,999 that I never made. Is this fraud? Please look into it right away.", "billing", "urgent", "neutral"),
    ("Parcel delayed again", "My parcel was supposed to be here Monday. Still nothing and tracking is stuck. Very disappointed.", "shipping", "medium", "frustrated"),
    ("Cannot log in on new phone", "I installed the app on my new phone but the sign in keeps showing an error.", "account", "medium", "neutral"),
    ("Feature suggestion", "It would be great to have an option to export my order history to Excel.", "product", "low", "neutral"),
    ("Subscription price increased", "My plan went from 499 to 599 without any notice. This is ridiculous, I'm cancelling.", "billing", "medium", "angry"),
    ("How do I download my data?", "I'd like a copy of all my personal data before I delete my account.", "account", "low", "neutral"),
    ("Support has not replied", "I raised a ticket four days ago and no one has responded. Please escalate.", "general", "high", "frustrated"),
    ("Wrong size shipped", "I ordered size M but received L. Can I get an exchange?", "shipping", "medium", "neutral"),
    ("App shows blank screen", "After logging in the home screen is completely blank. Restarting didn't help.", "technical", "high", "neutral"),
    ("Refund timeline", "How many days will the refund take to reach my bank account?", "billing", "low", "neutral"),
    ("Great service!", "Just wanted to say thank you to the agent who fixed my issue yesterday.", "general", "low", "positive"),
    ("Website down", "I cannot open your website at all, it shows a 502 error. We run our business through it, this is critical.", "technical", "urgent", "neutral"),
    ("Invoice download not working", "The download invoice button does nothing when I click it.", "technical", "medium", "neutral"),
    ("Order cancelled without notice", "My order was cancelled and nobody told me. I've now waited two weeks for it. Unbelievable.", "shipping", "high", "angry"),
    ("Change phone number", "Please update the phone number on my profile to my new one.", "account", "low", "neutral"),
]


def evaluate(dataset, triager: Triager | None = None) -> dict:
    triager = triager or RulesTriager()
    n = len(dataset)
    cat = sent = exact = near = 0
    misses = []
    confusion = Counter()
    for subject, description, want_cat, want_pri, want_sent in dataset:
        got = triager.triage(subject, description)
        ok_cat = got.category == want_cat
        ok_sent = got.sentiment.value == want_sent
        pri_gap = abs(LEVEL[got.priority] - LEVEL[TicketPriority(want_pri)])
        cat += ok_cat
        sent += ok_sent
        exact += pri_gap == 0
        near += pri_gap <= 1
        if not (ok_cat and ok_sent and pri_gap == 0):
            misses.append((subject, (want_cat, want_pri, want_sent), (got.category, got.priority.value, got.sentiment.value)))
        if not ok_cat:
            confusion[(want_cat, got.category)] += 1
    return {
        "n": n,
        "category": cat / n,
        "sentiment": sent / n,
        "priority_exact": exact / n,
        "priority_within_one": near / n,
        "misses": misses,
        "category_confusions": confusion,
    }


def report(name: str, r: dict) -> None:
    print(f"{name} ({r['n']} tickets)")
    print(f"  category  {r['category']:.0%}   sentiment {r['sentiment']:.0%}   "
          f"priority exact {r['priority_exact']:.0%}, within one level {r['priority_within_one']:.0%}")
    for subject, want, got in r["misses"]:
        print(f"  - {subject!r}\n      wanted {want}\n      got    {got}")


if __name__ == "__main__":
    heldout = "--heldout" in sys.argv
    report("HELD-OUT set" if heldout else "DEV set", evaluate(HELDOUT if heldout else DEV))
