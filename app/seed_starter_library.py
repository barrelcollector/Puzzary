from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from app.database import SessionLocal
from app.models import DictionaryDefinition, DictionaryWord, PuzzleTag, WordBank, WordBankWord


@dataclass(frozen=True)
class DefinitionSeed:
    part_of_speech: str
    definition: str
    clue: str
    example: str = ""


BANKS: dict[str, list[str]] = {
    "Weather Watch": ["cloud", "storm", "breeze", "thunder", "drizzle", "forecast", "rainbow", "humidity"],
    "Space Desk": ["planet", "comet", "orbit", "galaxy", "meteor", "rocket", "nebula", "eclipse"],
    "Ocean Roll": ["coral", "tide", "reef", "harbor", "anchor", "kelp", "current", "lagoon"],
    "Kitchen Drawer": ["whisk", "kettle", "skillet", "ladle", "pantry", "apron", "grater", "saucer"],
    "Garden Bed": ["tulip", "ivy", "mulch", "petal", "pollen", "trellis", "compost", "seedling"],
    "Office Kit": ["stapler", "memo", "binder", "folder", "marker", "invoice", "scanner", "clipboard"],
    "Music Room": ["rhythm", "melody", "chorus", "tempo", "fiddle", "octave", "cymbal", "ballad"],
    "Travel Bag": ["ticket", "hostel", "passport", "runway", "luggage", "itinerary", "compass", "terminal"],
    "Sports Page": ["racket", "inning", "goalie", "sprint", "hurdle", "jersey", "podium", "whistle"],
    "Science Lab": ["atom", "cell", "molecule", "beaker", "isotope", "gravity", "enzyme", "prism"],
    "Tech Stack": ["server", "router", "kernel", "widget", "browser", "script", "cache", "cursor"],
    "Home Shelf": ["attic", "porch", "faucet", "closet", "pillow", "mantel", "doormat", "outlet"],
    "Art Wall": ["canvas", "palette", "easel", "sketch", "fresco", "pastel", "gallery", "portrait"],
    "Book Cart": ["novel", "stanza", "chapter", "margin", "index", "preface", "library", "bookmark"],
    "School Day": ["recess", "quiz", "lecture", "campus", "pencil", "diploma", "lesson", "notebook"],
    "Money Matters": ["budget", "credit", "debit", "equity", "coupon", "tariff", "profit", "wallet"],
    "Health Desk": ["clinic", "pulse", "vaccine", "dosage", "bandage", "symptom", "therapy", "vitamin"],
    "Building Blocks": ["arch", "column", "facade", "atrium", "lintel", "dormer", "blueprint", "scaffold"],
    "Transit Map": ["bicycle", "engine", "tunnel", "bridge", "station", "trolley", "ferry", "caboose"],
    "Landscape": ["valley", "meadow", "canyon", "forest", "pebble", "glacier", "geyser", "prairie"],
    "Feeling Words": ["joyful", "solemn", "curious", "nervous", "gentle", "gloomy", "eager", "calm"],
    "Action Verbs": ["climb", "drift", "polish", "gather", "mingle", "stretch", "rescue", "whisper"],
    "Color Box": ["amber", "scarlet", "indigo", "violet", "silver", "copper", "ivory", "ebony"],
    "Shape Study": ["circle", "triangle", "spiral", "sphere", "cylinder", "diamond", "crescent", "lattice"],
    "Time Keeper": ["minute", "decade", "season", "sunset", "sunrise", "calendar", "moment", "midnight"],
    "Law Desk": ["verdict", "statute", "appeal", "counsel", "witness", "docket", "clause", "permit"],
    "Civic Words": ["senate", "mayor", "ballot", "census", "policy", "cabinet", "embassy", "treaty"],
    "Newsroom": ["headline", "editor", "source", "briefing", "press", "article", "byline", "edition"],
    "Florida Notes": ["orange", "mangrove", "keylime", "marina", "sunshine", "peninsula", "bayou", "palmetto"],
    "Miami Notes": ["skyline", "cafecito", "causeway", "beach", "highrise", "biscayne", "coconut", "wynwood"],
    "Morning Routine": ["coffee", "alarm", "oatmeal", "routine", "toaster", "commute", "shower", "agenda"],
    "Night Shift": ["moonlight", "lantern", "dream", "shadow", "starlight", "lullaby", "twilight", "silence"],
    "Game Table": ["puzzle", "token", "board", "quest", "score", "player", "tactic", "meeple"],
    "Print Shop": ["label", "cutter", "raster", "thermal", "spooler", "queue", "roller", "paper"],
    "Home Automation": ["sensor", "switch", "entity", "automation", "dashboard", "scene", "trigger", "helper"],
    "Network Print": ["ipp", "socket", "subnet", "gateway", "address", "hostname", "daemon", "protocol"],
    "Puzzle Types": ["crossword", "wordsearch", "sudoku", "cryptogram", "acrostic", "nonogram", "kakuro", "clue"],
    "Savory Bites": ["basil", "noodle", "olive", "pepper", "lentil", "muffin", "cereal", "yogurt"],
    "Sweet Shelf": ["brownie", "custard", "sorbet", "eclair", "cookie", "taffy", "parfait", "waffle"],
    "Clothes Rack": ["jacket", "collar", "pocket", "button", "zipper", "sleeve", "sandal", "scarf"],
    "Tool Bench": ["hammer", "wrench", "chisel", "pliers", "drill", "mallet", "clamp", "level"],
    "Workshop Shelf": ["bench", "vise", "sander", "lumber", "resin", "hinge", "dowel", "plywood"],
    "Holiday Box": ["wreath", "parade", "candle", "feast", "ribbon", "ornament", "carol", "confetti"],
    "Classical Set": ["atlas", "oracle", "temple", "lyre", "marble", "epic", "laurel", "amphora"],
    "Map Room": ["island", "delta", "plateau", "border", "capital", "region", "latitude", "meridian"],
    "Math Shelf": ["algebra", "fraction", "integer", "angle", "vector", "matrix", "theorem", "decimal"],
    "Language Lab": ["vowel", "grammar", "accent", "idiom", "suffix", "prefix", "syntax", "proverb"],
    "Photo Kit": ["camera", "lens", "shutter", "focus", "tripod", "exposure", "snapshot", "negative"],
    "Cinema Shelf": ["actor", "director", "cinema", "trailer", "cameo", "studio", "screen", "montage"],
    "Craft Box": ["yarn", "needle", "loom", "quilt", "pattern", "stencil", "glue", "bead"],
}


MULTIPLE_DEFINITIONS: dict[str, list[DefinitionSeed]] = {
    "cloud": [
        DefinitionSeed("Noun", "A visible mass of water droplets or ice crystals in the sky.", "These can be found in the sky and on the internet."),
        DefinitionSeed("Noun", "Remote computing or storage reached over a network.", "Not local storage, sometimes."),
    ],
    "current": [
        DefinitionSeed("Noun", "A steady movement of water or air in one direction.", "Flow in water or air."),
        DefinitionSeed("Adjective", "Happening or existing now.", "Up to date."),
    ],
    "cell": [
        DefinitionSeed("Noun", "The smallest basic unit of living things.", "Tiny unit of life."),
        DefinitionSeed("Noun", "One box in a table, grid, or spreadsheet.", "A single box in a grid."),
    ],
    "script": [
        DefinitionSeed("Noun", "Written text for a play, show, or program.", "Words for actors or automation."),
        DefinitionSeed("Verb", "To write instructions or dialogue.", "Write lines for a program or scene."),
    ],
    "season": [
        DefinitionSeed("Noun", "One of the main divisions of the year.", "Spring, summer, fall, or winter."),
        DefinitionSeed("Verb", "To add flavor with salt, herbs, or spices.", "Add flavor before cooking."),
    ],
    "appeal": [
        DefinitionSeed("Noun", "A request for a higher court to review a decision.", "Legal request for review."),
        DefinitionSeed("Verb", "To attract or interest someone.", "Seem attractive."),
    ],
    "press": [
        DefinitionSeed("Noun", "News organizations considered together.", "The news media."),
        DefinitionSeed("Verb", "To push firmly against something.", "Push down."),
    ],
    "orange": [
        DefinitionSeed("Noun", "A citrus fruit with a tough peel.", "Florida citrus fruit."),
        DefinitionSeed("Adjective", "Having a color between red and yellow.", "Like a sunset or pumpkin."),
    ],
    "switch": [
        DefinitionSeed("Noun", "A control that turns something on or off.", "On-off control."),
        DefinitionSeed("Verb", "To change from one thing to another.", "Change places or settings."),
    ],
    "board": [
        DefinitionSeed("Noun", "A flat piece of material or a game surface.", "Flat game surface."),
        DefinitionSeed("Verb", "To get onto a vehicle.", "Get on a train or plane."),
    ],
    "label": [
        DefinitionSeed("Noun", "A small piece of material that identifies something.", "What the Zebra printer makes."),
        DefinitionSeed("Verb", "To mark something with a name or description.", "Put a name on it."),
    ],
    "level": [
        DefinitionSeed("Noun", "A stage or rank in a game or system.", "A game stage."),
        DefinitionSeed("Adjective", "Flat, even, or balanced.", "Not slanted."),
    ],
    "permit": [
        DefinitionSeed("Noun", "An official document giving permission.", "Official permission paper."),
        DefinitionSeed("Verb", "To allow something to happen.", "Allow."),
    ],
    "capital": [
        DefinitionSeed("Noun", "A city where a government is based.", "Seat of government."),
        DefinitionSeed("Noun", "Money or assets used for investment.", "Money used to start or grow work."),
    ],
    "focus": [
        DefinitionSeed("Noun", "The sharpness or center of attention in an image or idea.", "Sharpness in a photo."),
        DefinitionSeed("Verb", "To concentrate attention or adjust sharpness.", "Concentrate or sharpen."),
    ],
}


SPECIFIC_DEFINITIONS: dict[str, DefinitionSeed] = {
    "actor": DefinitionSeed("Noun", "A performer who plays a role in a film, show, or play.", "Performer in a film or play."),
    "director": DefinitionSeed("Noun", "The person who guides the creative work of a film or stage production.", "Person guiding a film's creative work."),
    "cinema": DefinitionSeed("Noun", "Movies considered as an art form, industry, or place to watch films.", "The world of movies."),
    "trailer": DefinitionSeed("Noun", "A short preview used to advertise a movie or show.", "Movie preview."),
    "cameo": DefinitionSeed("Noun", "A brief appearance by a known person in a movie or show.", "Brief guest appearance."),
    "studio": DefinitionSeed("Noun", "A company or space where movies, shows, or recordings are made.", "Place or company that makes films."),
    "screen": DefinitionSeed("Noun", "The surface where a movie or image is shown.", "Where the movie appears."),
    "montage": DefinitionSeed("Noun", "A sequence of short shots edited together to show time, action, or mood.", "Rapid sequence of edited shots."),
    "bandage": DefinitionSeed("Noun", "A strip or wrap used to cover and protect an injured area.", "Wound covering."),
    "cafecito": DefinitionSeed("Noun", "A small, strong Cuban-style coffee, often sweetened.", "Small Cuban coffee."),
    "quiz": DefinitionSeed("Noun", "A short test or set of questions.", "Short test."),
    "wynwood": DefinitionSeed("Noun", "A Miami neighborhood known for murals, galleries, and street art.", "Miami arts district known for murals."),
}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def get_or_create_tag(db, tag_type: str, name: str) -> PuzzleTag:
    tag_type = tag_type.strip() or "General"
    name = name.strip()
    tag = db.scalar(select(PuzzleTag).where(PuzzleTag.tag_type == tag_type, PuzzleTag.name == name))
    if tag:
        return tag
    tag = PuzzleTag(tag_type=tag_type, name=name, slug=slugify(f"{tag_type}-{name}"))
    db.add(tag)
    db.flush()
    return tag


def starter_definition(word: str, bank_name: str) -> DefinitionSeed:
    if word in SPECIFIC_DEFINITIONS:
        return SPECIFIC_DEFINITIONS[word]
    clue = f"{bank_name} term starting with {word[0].upper()} ({len(word)} letters)."
    if bank_name == "Action Verbs":
        return DefinitionSeed("Verb", f"To {word.lower()} as an action.", clue)
    if bank_name in {"Feeling Words", "Color Box"}:
        return DefinitionSeed("Adjective", f"Describing something as {word.lower()}.", clue)
    return DefinitionSeed("Noun", f"A term connected with {bank_name.lower()}.", clue)


def ensure_definition_tags(db, definition: DictionaryDefinition, bank_name: str):
    definition.tags = [
        get_or_create_tag(db, "Part of Speech", definition.part_of_speech),
        get_or_create_tag(db, "Clue", definition.properties.get("clue", "")),
        get_or_create_tag(db, "Theme", bank_name),
    ]


def seed():
    all_words = [word for words in BANKS.values() for word in words]
    duplicates = sorted({word for word in all_words if all_words.count(word) > 1})
    if duplicates:
        raise RuntimeError(f"Duplicate seed words: {', '.join(duplicates)}")
    if len(all_words) != 400:
        raise RuntimeError(f"Expected 400 words, found {len(all_words)}")

    with SessionLocal() as db:
        source_tag = get_or_create_tag(db, "Source", "Puzzary Starter Library")
        words_created = 0
        definitions_created = 0
        banks_created = 0
        links_created = 0

        for bank_position, (bank_name, words) in enumerate(BANKS.items(), start=1):
            bank = db.scalar(select(WordBank).where(WordBank.slug == slugify(bank_name)))
            if bank is None:
                bank = WordBank(
                    name=bank_name,
                    slug=slugify(bank_name),
                    description=f"Starter word bank for {bank_name.lower()} puzzles.",
                    payload={"mode": "manual", "source": "puzzary_starter_library", "seed_position": bank_position},
                )
                db.add(bank)
                db.flush()
                banks_created += 1
            else:
                bank.name = bank_name
                bank.description = bank.description or f"Starter word bank for {bank_name.lower()} puzzles."
                bank.payload = {**(bank.payload or {}), "mode": "manual", "source": "puzzary_starter_library"}

            theme_tag = get_or_create_tag(db, "Theme", bank_name)
            bank.tags = list({tag.id: tag for tag in [source_tag, theme_tag]}.values())

            for position, word_text in enumerate(words, start=1):
                normalized = word_text.lower()
                word = db.scalar(select(DictionaryWord).where(DictionaryWord.word == normalized))
                if word is None:
                    primary = (MULTIPLE_DEFINITIONS.get(normalized) or [starter_definition(normalized, bank_name)])[0]
                    word = DictionaryWord(
                        word=normalized,
                        display_text=word_text,
                        clue=primary.clue,
                        part_of_speech=primary.part_of_speech,
                        properties={"starter_bank": bank_name},
                    )
                    db.add(word)
                    db.flush()
                    words_created += 1
                word.tags = list({tag.id: tag for tag in [source_tag, theme_tag]}.values())

                existing_definitions = {
                    (definition.part_of_speech, definition.definition): definition for definition in word.definitions
                }
                seeds = MULTIPLE_DEFINITIONS.get(normalized) or [starter_definition(normalized, bank_name)]
                first_definition = None
                for definition_position, definition_seed in enumerate(seeds, start=1):
                    definition = existing_definitions.get((definition_seed.part_of_speech, definition_seed.definition))
                    if definition is None:
                        definition = next(
                            (
                                item
                                for item in word.definitions
                                if item.source == "puzzary_starter_library"
                                and (item.properties or {}).get("starter_bank") == bank_name
                                and str(item.definition).startswith("A term connected with ")
                            ),
                            None,
                        )
                    if definition is None:
                        definition = DictionaryDefinition(
                            word=word,
                            part_of_speech=definition_seed.part_of_speech,
                            definition=definition_seed.definition,
                            example=definition_seed.example,
                            source="puzzary_starter_library",
                            position=definition_position,
                            properties={"clue": definition_seed.clue, "starter_bank": bank_name},
                        )
                        db.add(definition)
                        db.flush()
                        definitions_created += 1
                    else:
                        definition.definition = definition_seed.definition
                        definition.part_of_speech = definition_seed.part_of_speech
                        definition.example = definition_seed.example
                        definition.position = definition_position
                        definition.source = "puzzary_starter_library"
                        definition.properties = {
                            **(definition.properties or {}),
                            "clue": definition_seed.clue,
                            "starter_bank": bank_name,
                        }
                    ensure_definition_tags(db, definition, bank_name)
                    if first_definition is None:
                        first_definition = definition

                link = db.scalar(
                    select(WordBankWord).where(WordBankWord.word_bank_id == bank.id, WordBankWord.word_id == word.id)
                )
                if link is None:
                    db.add(WordBankWord(word_bank=bank, word=word, definition=first_definition, position=position))
                    links_created += 1
                else:
                    link.definition = first_definition
                    link.position = position

        db.commit()
        print(
            "Seed complete: "
            f"{words_created} words, {definitions_created} definitions, "
            f"{banks_created} word banks, {links_created} bank links created."
        )


if __name__ == "__main__":
    seed()
