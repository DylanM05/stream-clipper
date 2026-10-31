
## Breakdown

The show tracks what each stream's time goes on, so return `breakdown`: the whole stream split into parts, in order, covering it from start to end with no gaps or overlaps, each under one of the show's breakdown categories. They're under "# Breakdown categories" in the input, each with what it covers. Anything none of them covers is `Other`.

- Go by what's actually being said or done in each stretch, as the categories describe it. Where more than one fits, pick the one that fits most of the stretch.
- Follow the stream closely: start a new part wherever the category changes, even for a minute or two in the middle of a chapter, and merge neighbouring parts of the same category. Parts under 30 seconds aren't worth splitting off.
- Category: the category's name exactly as given, or `Other`.
- Note: what happens in it, in a few plain words, e.g. "Reads the latest headlines" or "Sponsor read for a VPN". Factual and neutral, like the summaries.
- Stretches with no talking show up as gaps between the transcript's timestamps. Put them in a neighbouring part, unless one of the categories is for them.

Also return `other_summary`: one or two plain sentences on what the parts under `Other` were mostly about, so the show can see what its categories miss, e.g. "Chatting with viewers about his weekend, and a long stretch playing a racing game." Factual and neutral, like the notes. Empty if no part is `Other`.
