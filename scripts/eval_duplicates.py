"""Calibrate and test the duplicate-ticket similarity threshold.

Run:  python -m scripts.eval_duplicates
CALIBRATION pairs are used to choose DUP_MIN_SCORE. TEST pairs were written before looking at
any scores and are only used to report how the chosen threshold generalises.
Each pair is (ticket A text, ticket B text, are_duplicates).
"""
import numpy as np

from app.core.config import settings
from app.services.duplicates import text_similarity

CALIBRATION = [
    ("Cannot reset my password. The reset email never arrives.", "Password reset link not coming. I requested a reset but no email received.", True),
    ("Charged twice. I see two charges on my card this month.", "Double charged for subscription. My card was billed two times this month.", True),
    ("Where is my order? Tracking has not updated in 8 days.", "Parcel not delivered. My order tracking is stuck since last week.", True),
    ("App crashes on startup. It closes right after the splash screen.", "App keeps crashing when I open it. It closes immediately after launching.", True),
    ("Refund not received. Approved two weeks ago, nothing arrived.", "Still waiting for my refund. Refund approved last week but money not in my account.", True),
    ("Two-factor code invalid. The authenticator code is always rejected.", "Two factor not working. The verification code is rejected every time.", True),
    ("Photo upload fails. The upload spins forever for large images.", "Cannot upload picture. Uploading a big image never finishes.", True),
    ("Wrong item delivered. I ordered a blue jacket and got a black one.", "Received incorrect product. I got a black jacket instead of the blue one I ordered.", True),
    ("Cannot reset my password. The reset email never arrives.", "Refund not received. Approved two weeks ago, nothing arrived.", False),
    ("Where is my order? Tracking has not updated in 8 days.", "App crashes on startup. It closes right after the splash screen.", False),
    ("Invoice needs GST number. Please re-issue invoice 4821.", "Delivery address change. Can I change the address before it ships?", False),
    ("Need to cancel my plan. Please cancel at the end of the billing period.", "Photo upload fails. The upload spins forever for large images.", False),
    ("Charged twice. I see two charges on my card this month.", "Need to cancel my plan. Please cancel at the end of the billing period.", False),
    ("Wrong item delivered. I ordered a blue jacket and got a black one.", "Delivery address change. Can I change the address before it ships?", False),
    ("Two-factor code invalid. The authenticator code is always rejected.", "How do I change my email address? I want to move to a new email.", False),
    ("Refund not received. Approved two weeks ago, nothing arrived.", "Charged twice. I see two charges on my card this month.", False),
]

TEST = [
    ("Locked out of account. Too many wrong attempts and now I cannot sign in.", "Account locked. I entered the wrong password several times and it will not let me log in.", True),
    ("Subscription charged again after cancelling. I cancelled but was billed today.", "Billed after cancellation. I cancelled my plan last month yet a new charge appeared.", True),
    ("Package damaged. The box arrived crushed and the item is broken.", "Item broken on arrival. My parcel was crushed and what is inside is damaged.", True),
    ("Website very slow. Pages take forever to load on my phone.", "Site loading slowly. Every page takes ages to open on mobile.", True),
    ("Need invoice with company name. Please add our GST number to the invoice.", "Invoice correction. Can you reissue my invoice including our company GST details?", True),
    ("Delete my account. Please remove all my data.", "Account deletion request. I want my account and personal data erased.", True),
    ("Locked out of account. Too many wrong attempts and now I cannot sign in.", "Subscription charged again after cancelling. I cancelled but was billed today.", False),
    ("Package damaged. The box arrived crushed and the item is broken.", "Website very slow. Pages take forever to load on my phone.", False),
    ("Need invoice with company name. Please add our GST number to the invoice.", "Refund timeline. How many days will my refund take to reach my bank?", False),
    ("Delete my account. Please remove all my data.", "Where is my parcel? The courier tracking has not moved for a week.", False),
    ("Subscription charged again after cancelling. I cancelled but was billed today.", "Refund timeline. How many days will my refund take to reach my bank?", False),
    ("Website very slow. Pages take forever to load on my phone.", "App crashes on startup. It closes right after the splash screen.", False),
]


def scores(pairs):
    return [(text_similarity(a, b), dup) for a, b, dup in pairs]


def prf(sc, threshold):
    tp = sum(1 for s, d in sc if d and s >= threshold)
    fp = sum(1 for s, d in sc if not d and s >= threshold)
    fn = sum(1 for s, d in sc if d and s < threshold)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return precision, recall, tp, fp, fn


if __name__ == "__main__":
    cal, test = scores(CALIBRATION), scores(TEST)
    dup = sorted(s for s, d in cal if d)
    non = sorted((s for s, d in cal if not d), reverse=True)
    print("CALIBRATION similarity of true duplicates (low to high):", [round(s, 2) for s in dup])
    print("CALIBRATION similarity of non-duplicates (high to low):", [round(s, 2) for s in non][:8])
    print(f"\nthreshold  precision  recall   (calibration)   precision  recall   (test)")
    for t in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50):
        pc, rc, *_ = prf(cal, t)
        pt, rt, *_ = prf(test, t)
        mark = "  <- current" if abs(t - settings.dup_min_score) < 1e-9 else ""
        print(f"  {t:.2f}       {pc:.0%}       {rc:.0%}                         {pt:.0%}       {rt:.0%}{mark}")
