# Globus Search in pcldb

Back to the [README](../README.md).

## First, a correction: what Globus Search is and is not

Globus Search **does not read the files on ARC** and does not look into the project folder. It is
a search index that Globus runs on its own servers. It holds small JSON entries that we send it,
and it searches only those. If an entry was never sent, Globus Search does not know the session
exists, whatever is on ARC.

So pcldb does two things at every registration: it writes the session into the database, and it
queues the session's entry. Nothing leaves this computer until someone runs `pcldb search push`.

Today **there is no live index**. A stand-in on this computer answers every search: it takes the
same requests, evaluates them over the queued entries, and answers in the same shape as the
service. The code for the real service is written and was tested against a fake only.

## How it fits together

```text
registration --> the database (pcl.sessions)
             \-> the queue (app.search_entries, state "pending")
                     |
                     |  pcldb search push   (needs: pcldb search login, an index)
                     v
               Globus Search index  (states: sent -> synced, or failed)

a search (website, pcldb ask, pcldb search query, POST /api/v1/search)
   -> pcldb checks the request (the same checker for both backends)
   -> search.backend = "local":  the stand-in reads the queue on this computer
      search.backend = "globus": Globus Search answers; if it cannot be reached and
                                 search.fallback_to_local is true, the stand-in answers
                                 and the result says so
```

The states of a queued entry: `pending` (waits to be sent), `sent` (handed over, not yet
confirmed), `synced` (in the index), `failed` (the service refused it; `pcldb search status` says
why), `delete_pending` (the session was removed; the next push deletes it from the index).
`pcldb search status` counts them.

## The entry format

Every session has one entry:

```json
{"subject": "urn:pcl:dataset:...",
 "visible_to": ["urn:globus:groups:id:YOUR_LAB_GROUP_ID"],
 "content": {
   "schema": "pcl-search/1",
   "title": "1d_y_scan_20261119_150917_113",
   "instrument": "raman-portable",
   "folder": "1d_y_scan_20261119_150917_113",
   "session_id": "demo-session-0002",
   "path": "raw/raman-portable/simulated/1d_y_scan_20261119_150917_113",
   "collection_id": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
   "data_path": "/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261119_150917_113",
   "globus_url": "globus://8e804f3a-5bac-4b6d-9836-bcd5b9c133ec/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261119_150917_113/",
   "web_url": "https://app.globus.org/file-manager?origin_id=8e804f3a-5bac-4b6d-9836-bcd5b9c133ec&origin_path=%2Fpcl_photonics%2Fraw%2Framan-portable%2Fsimulated%2F1d_y_scan_20261119_150917_113%2F",
   "method": "1d-y", "completion": "complete", "format": "raman-session/1 folder",
   "location": "demo-bench-1", "review_state": "pending",
   "sample_ids": ["DEMO-S-102"], "tags": ["demo"],
   "labels": ["UNKNOWN", "adenine"],
   "label_counts": [{"count": 3, "label": "UNKNOWN"}, {"count": 10, "label": "adenine"}],
   "models": ["demo_pca_svm_20260922"],
   "simulated": true, "is_copy": false, "withdrawn": false, "corrected": false,
   "items": 13, "items_planned": 13, "files": 17, "bytes": 576619, "n_needs_review": 1,
   "started": "2026-11-19T15:09:17Z", "uploaded": "2026-11-19T15:57:17Z",
   "verified": "2026-11-20T23:05:24Z", "last_run": "2026-11-19T17:09:38Z"}}
```

- **subject** is `urn:pcl:dataset:` and a UUID computed from the collection id and the folder's
  path inside the collection (the same rule as the proof of concept `pcl_index`). The same folder
  always gets the same subject. The subject itself is not searched.
- **content** fields and their types are fixed in `pcldb/schemas/search_fields.json`. Globus
  Search fixes a field's type the first time it sees it, so a type there never changes; a later
  version only adds fields. Every entry is checked against that file before it is queued.

| Type | Fields |
|---|---|
| text (exact match) | `schema`, `title`, `instrument`, `folder`, `session_id`, `path`, `collection_id`, `data_path`, `globus_url`, `web_url`, `method`, `completion`, `format`, `location`, `review_state`, `operator` (only when switched on) |
| free text | `notes` (only when switched on) |
| list of texts | `sample_ids`, `tags`, `labels`, `models`, `problems` |
| list of `{label, count}` | `label_counts` (search it as `label_counts.label` and `label_counts.count`) |
| true/false (always there) | `simulated`, `is_copy`, `withdrawn`, `corrected` |
| whole number | `items`, `items_planned`, `files`, `bytes`, `n_needs_review` |
| date (UTC to the second, with Z) | `started`, `uploaded`, `verified` (only when the newest check passed), `last_run` |

Empty values (null, `""`, `[]`) are left out. `operator` and `notes` are personal and stay out
unless the settings `search.include_operator` / `search.include_notes` are true. Globus Search is
outside the university's High Assurance coverage, so nothing sensitive belongs in an entry.

**visible_to** comes from the setting `search.visible_to` at the time the entry is queued (see
step 4 below for what that means after a change). When it is empty (the default), the
entry is sent visible only to the Globus identity that pushes it. `public` and
`all_authenticated_users` are refused: entries are never public. To let the lab see the entries,
put the lab's Globus group there, for example `["urn:globus:groups:id:YOUR_LAB_GROUP_ID"]`.

In the demo data an entry is about 1.2 KB of JSON. How Globus counts an index's size is not
documented; `pcldb search status` shows the size the service reports.

## The request language (the part pcldb accepts)

A request is a Globus Search `GSearchRequest`, version `query#1.0.0`, with fewer options. One
checker is used before either backend, so a request means the same on both. Anything else is
refused with a message that names the key or the field.

| Key | Accepted |
|---|---|
| `@version` | only `"query#1.0.0"` (filled in when left out) |
| `q` | text, at most 256 characters; `"*"` means everything; needed unless `filters` is given |
| `q_settings` | only `{"mode": "query_string", "default_operator": "and"}` or `"or"` |
| `limit` | 0 to 100, default 10 (0 = counts and facets only) |
| `offset` | 0 to 9999, and `offset` + `limit` at most 10000 |
| `filters` | at most 32, all of them must hold |
| `facets` | at most 32, only `{"name", "type": "terms", "field_name", "size"}` (size 1 to 100, default 10) |
| `sort` | at most 8, `{"field_name", "order": "asc" or "desc"}` (`desc` when left out) |

Filters:

- `{"type": "match_any", "field_name": "labels", "values": ["adenine", "r6g"]}`: one value must
  match exactly (whole value, letter case counts). `match_all`: every value must. On a list
  field any element may match. Values are texts, or `true`/`false` on true/false fields.
- `{"type": "range", "field_name": "started", "values": [{"from": "2026-11-01T00:00:00Z", "to": "*"}]}`:
  inclusive; `"*"` is an open end; `gte`, `gt`, `lte`, `lt` also work; several ranges are
  alternatives. Only on whole-number and date fields; dates are always written
  `YYYY-MM-DDTHH:MM:SSZ`. Whole-number fields take `range`, never `match_any`.
- `{"type": "exists", "field_name": "verified"}`, `{"type": "not", "filter": {...}}`,
  `{"type": "and" | "or", "filters": [...]}`.
- Not accepted: `like`, the geo filters, `advanced`, `boosts`, `fields`, `post_facet_filters`,
  `filter_principal_sets`, `bypass_visible_to`, facet types other than `terms`.

pcldb is stricter than the service on purpose: the service answers an unknown field with zero
results and no error; pcldb refuses it, so a typing error is not mistaken for "no data".

Try a request against the stand-in:

```sh
./pcldb.sh search query --request '{"q": "*", "limit": 5}'
./pcldb.sh search query --request @request.json
```

## The stand-in and the real service: the differences

The stand-in copies the service's behaviour as it was observed on 2026-11-20 (exact matches,
how words in `q` are matched, the result shape). The known differences (the full list is at the
top of `pcldb/search_local.py`):

1. **Who may see an entry.** The service shows each person only the entries whose `visible_to`
   fits their Globus identity. The stand-in shows every entry to everyone who may search in
   pcldb.
2. **When an entry can be found.** The stand-in: as soon as it is queued. The service: after the
   push and its ingest task finished.
3. **How `q` cuts text into words** is not documented by Globus. The stand-in assumes the usual
   rules: the underscore stays inside a word, so `1d_y_scan_20261116` and `methylene_blue` are
   each one word (find them with `1d_y*` or the whole word), and `raman-portable` is two words.
   Known values work best as filters, which the assistant and the keyword rules already use.
4. With `default_operator` `and`, the stand-in wants every word somewhere in the entry; the
   service may want them in one field.
5. `q` looks at text values only; the service stores dates as dates, so a word like `2026` can
   match differently there.
6. The special signs of the service's query syntax (`+`, `-`, `|`, `?`, `~`, a `*` inside a word)
   are plain separators in the stand-in.
7. Order without `sort`: the service orders by its own relevance score; the stand-in by the
   number of matched words, then `started` newest first, then subject.
8. `sort` on a field no entry has ever had: the service answers 400; the stand-in puts every
   entry last.
9. Texts longer than 10,000 characters, the service's rate limits and its 10 MB request limit
   are not copied.

## Going live, step by step

This needs the lab lead's own Globus account (the one that can see the ARC Projects
collection). Do it on the computer that runs pcldb.

1. Install the Globus package (once):

   ```sh
   python3 install.py --globus
   ```

   (Windows: `py -3 install.py --globus`.) `./pcldb.sh doctor` then says `globus-sdk: installed`.

2. Sign in to Globus:

   ```sh
   ./pcldb.sh search login
   ```

   It opens the browser at the Globus sign-in page and asks for consent to use Globus Search.
   On a computer without a browser, `./pcldb.sh search login --no-browser` prints an address to
   open elsewhere and asks for the code it shows. The sign-in is saved in your personal folder
   (`~/.pcldb/globus_tokens.json`) and renewed by itself. It uses the public app id of the Globus
   SDK tutorial (`61338d24-54d5-408f-a10d-66c06b59f6d2`, the same as the Raman controller) unless
   the setting `search.client_id` names the lab's own app, registered at app.globus.org.

3. Make the index:

   ```sh
   ./pcldb.sh search create-index
   ```

   It makes a **trial** index with a bland name and description (anyone who knows the index id
   can read those and the counts, never the entries), saves its id as `search.index_id` in
   `pcldb_config.json`, and queues every entry for it. `--name TEXT` and `--description TEXT`
   choose others; keep them bland.

4. Decide who may see the entries. To share them with the lab, put the lab's Globus group into
   `pcldb_config.json`:

   ```json
   {"search": {"visible_to": ["urn:globus:groups:id:YOUR_LAB_GROUP_ID"]}}
   ```

   (Merge it into the file's `"search"` group; keep the `index_id` that step 3 wrote.) Left empty,
   only you can see them.

   An entry takes `visible_to` when it is queued, that is when its session is registered or
   loaded. After changing `search.visible_to` (or `search.include_operator` /
   `search.include_notes`), run `./pcldb.sh search reindex`: it builds every entry again with the
   current settings and queues it, and the next push sends them. Entries queued with an empty
   list are sent visible to you alone.

5. Send the entries and look at the result:

   ```sh
   ./pcldb.sh search push
   ./pcldb.sh search status
   ```

   `push` sends what waits, in batches, and waits up to 60 seconds (`--wait SECONDS`) for Globus
   to confirm. Entries not confirmed by then stay `sent`; the next push asks again. `status`
   shows the counts, the reasons of failed entries, and the index's size and limit when the
   network answers.

6. Let Globus Search answer the searches:

   ```sh
   ./pcldb.sh search use-globus
   ```

   Then stop `pcldb serve` (Ctrl+C) and start it again: the server reads the settings when it
   starts. When Globus cannot be reached, the stand-in answers and the result says so (setting
   `search.fallback_to_local`). `./pcldb.sh search use-local` goes back.

7. After every registration (or once a day), run `./pcldb.sh search push`. Nothing is sent by
   itself.

With the Globus backend, the website's searches run with the Globus sign-in saved on the
computer that runs pcldb, so every website user sees what that one identity may see.
`./pcldb.sh search logout` cancels that sign-in and removes it from this computer.

To check the live service against the stand-in once, there is an opt-in test (it needs the
sign-in and a trial index made for it):

```sh
PCLDB_LIVE_GLOBUS=1 PCLDB_LIVE_GLOBUS_INDEX=YOUR_TEST_INDEX_ID .venv/bin/python -B -m unittest tests.test_search_local -v
```

## The limits of a trial index

- A person may own **3** trial indexes, and a trial index has one owner.
- Globus may **delete a trial index at any time from 30 days after it was made**. The database
  stays the record: make a new index (take `search.index_id` out of `pcldb_config.json`, run
  `create-index` again) and push; `create-index` queues everything again by itself, and
  `./pcldb.sh search reindex` does the same for an index that was emptied.
- The size limit is small and not the same for every trial index (1 MB and 5000 MB have both
  been seen). `pcldb search status` shows the limit the service reports and warns above 80 %.
- A larger, lasting index needs Globus support to convert it and attach it to a subscription
  (Virginia Tech's subscription is held by ARC).

## What was and was not checked

- Checked: the request checker, the stand-in, the queue, and the code for the real service
  against a fake Globus (no network) in the automated tests; the result shape and the behaviour
  the stand-in copies against public indexes of the real service (research, 2026-11-20).
- Not checked: signing in, making an index, sending entries, deleting entries or searching with
  the real service through pcldb. How the service cuts text into words (difference 3) is the
  first thing the live test will settle.
