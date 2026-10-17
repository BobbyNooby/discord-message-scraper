# Your word lists

Drop `*.txt` files here, one word per line. They are merged after the packaged
packs, so a word here always wins.

`config.example.toml` ships `[stopwords] dir = "./stopwords.d"`, which is why
this directory exists: a path in the example has to resolve, and the loader
refuses a `dir` that is not there. Delete the setting and it falls back to
`<project>/stopwords.d` — the same place.
