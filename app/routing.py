"""Conservative product routing; uncertainty triggers clarification, not a guess."""
import re

from app.models import Answer, Product, Question

STRONG = {
    "car": r"\b(?:car|motor|vehicle|motorcycle)\s+insurance\b|\bctp\b|\broadside assistance\b|\bcomprehensive essentials\b",
    "home": r"\b(?:home|house|building|buildings|contents|landlord|renters?)\s+insurance\b|\bhome and contents\b",
    "business": r"\b(?:business|commercial)\s+insurance\b|\bpublic(?: and products)? liability\b|\bworkers? compensation\b|\bcommercial\s+(?:motor\s+)?(?:vehicles?|cars?|motorcycles?|motor)\s+insurance\b",
}
CONTEXT = {
    "car": r"\b(?:cars?|vehicles?|motorcycles?|windscreens?|evs?)\b",
    "home": r"\b(?:homes?|houses?|buildings?|contents|landlords?|renters?|homeowners?)\b",
    "business": r"\b(?:business(?:es)?|commercial|workplaces?|tradies?)\b",
}
WORDS = {"car": "car", "motor": "car", "vehicle": "car", "home": "home",
         "house": "home", "business": "business", "commercial": "business"}


def infer_products(question: str) -> set[Product]:
    text = re.sub(r"[^\w\s&]", " ", question.casefold())
    text = " ".join(text.split())
    car_text = re.sub(r"\bcommercial\s+(?:motor\s+)?(?:vehicles?|cars?|motorcycles?|motor)\s+insurance\b", "", text)
    strong = {name for name, pattern in STRONG.items()
              if re.search(pattern, car_text if name == "car" else text)}
    # Shared suffix: 'car and home insurance' explicitly names both products.
    for match in re.finditer(r"\b(car|motor|vehicle|home|house|business|commercial)\s+"
                             r"(?:and|or|vs|versus|&)\s+"
                             r"(car|motor|vehicle|home|house|business|commercial)\s+insurance\b", text):
        strong.update(WORDS[word] for word in match.groups())
    if strong:
        return strong
    contextual = {name for name, pattern in CONTEXT.items() if re.search(pattern, text)}
    if contextual == {"car", "home"} and re.search(r"\bat home\b", text):
        return {"car"}
    # Strata can refer to home or commercial property; do not assign it silently.
    if not contextual and re.search(r"\bstrata\b", text):
        return {"home", "business"}
    return contextual


def infer_variant(text: str, product: Product | None) -> str | None:
    text = " ".join(re.findall(r"\w+", text.casefold()))
    if product == "car":
        found = set()
        if "comprehensive essentials" in text:
            found.add("comprehensive-essentials")
            text = text.replace("comprehensive essentials", "")
        if "comprehensive" in text:
            found.add("comprehensive")
        if "third party" in text:
            found.add("third-party")
        return next(iter(found)) if len(found) == 1 else None
    if product == "home":
        if "home and contents" in text or "building and contents" in text:
            return "home-and-contents"
        found = {label for label in ("building", "contents", "landlord")
                 if re.search(rf"\b{label}s?\b", text)}
        return next(iter(found)) if len(found) == 1 else None
    return None


def clarify_scope(request: Question) -> Answer | None:
    """Cheap scope checks only; no model calls or claims about policy coverage."""
    text = " ".join(re.findall(r"\w+", request.question.casefold()))
    product = request.product
    if product is None:
        return Answer(status="clarification_required", clarification_kind="product",
            answer="Is your question about car, home or business insurance?",
            choices=["Car insurance", "Home insurance", "Business insurance"])
    if product == "car":
        incident = re.search(r"\b(?:broke|broken|breakdown|damaged|crashed)\b|\b(?:won t|not|doesn t) start\b", text)
        assistance = re.search(r"\b(?:roadside|tow|towing|assistance|claims?|contact|phone|number)\b", text)
        coverage = re.search(r"\b(?:cover|covered|coverage|insured)\b|\brepair costs?\b|\bpay for\b", text)
        cause = re.search(r"\b(?:mechanical|engine|battery|accident|collision|crash|crashed|hit|theft|stolen|fire|flood|storm|breakdown)\b|\bbroke down\b|\b(?:won t|not|doesn t) start\b|\buser described incident\b", text)
        if incident and not assistance and not coverage:
            return Answer(status="clarification_required", clarification_kind="intent",
                answer="Do you need roadside assistance, want to check whether insurance covers repairs, or want to make a claim?",
                choices=["Roadside assistance", "Repair coverage", "Making a claim"])
        if incident and coverage and not cause and not assistance:
            return Answer(status="clarification_required", clarification_kind="incident",
                answer="Is this a mechanical breakdown, damage from an accident, or something else?",
                choices=["Mechanical breakdown", "Accident damage", "Something else"])
    # Specific questions retain their normal retrieval path. Only generic cover
    # questions need a plan/topic before we can meaningfully narrow the search.
    if re.search(r"\b(?:compare|comparison|types|differences)\b", text):
        return None
    words = set(text.split())
    fillers = {"what", "which", "does", "do", "is", "are", "my", "our", "your", "allianz",
               "car", "home", "house", "business", "insurance", "policy", "policies", "plan",
               "the", "a", "an", "me", "for", "under", "with", "and", "tell", "about", "more",
               "information", "details", "can", "you", "get", "i", "it"}
    cover_words = {"cover", "covers", "covered", "coverage", "include", "includes", "included", "benefits", "benefit"}
    remaining = words - fillers
    if not remaining or not remaining <= cover_words or not remaining & cover_words:
        return None
    if request.variant or infer_variant(request.question, product):
        return None
    if product == "car":
        return Answer(status="clarification_required", clarification_kind="variant",
            answer="Which car insurance cover do you mean? If you are not sure, I can look up the comparison.",
            choices=["Comprehensive", "Comprehensive Essentials", "Third Party Property Damage", "Compare the options"])
    if product == "home":
        return Answer(status="clarification_required", clarification_kind="variant",
            answer="Which home insurance cover do you mean?",
            choices=["Building", "Contents", "Home and contents", "Landlord", "Compare the options"])
    if product == "business":
        return Answer(status="clarification_required", clarification_kind="topic",
            answer="Which part of business insurance would you like to check?",
            choices=["Public liability", "Business property", "Commercial motor", "Workers compensation"])
    return None
