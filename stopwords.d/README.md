# Your word lists

Drop `*.txt` files here, one word per line. They are merged after the packaged
packs, so a word here always wins.

`config.example.toml` ships `[stopwords] dir = "./stopwords.d"`, which is why
this directory exists. The loader does not require it, though: a `dir` that is
not there is ignored with a warning, and the packaged packs still load. Delete
the setting and it looks for `<directory holding config.toml>/stopwords.d` — the
same place in a checkout.
