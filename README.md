# Monitoring · SisuNymous

Live: **https://fip-195-148-31-126.kaj.poutavm.fi:8790/**

Submit a RAN `.parquet`. It is turned into groups of at least 100 people. The desk ranks video, energy, coverage and mobility. SisuNymous answers only from that release.

![Desk](docs/desk.png)

## Results (mock file, k = 100)

- 1,099,340 rows → 515 groups. Smallest group: 137 people.
- 61% of video sessions still on 4G. Video should not sit on 2G.
- 8 of 65 sampled towns have no 3.5 GHz 5G. South Karelia is first.
- 2G is on at every sampled place and carried 0.19 GB in the whole window. That is wasted energy.
- Singling out and joining a person back did not work. Guessing from a group fact stays open.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

Open http://127.0.0.1:8790 and submit a RAN file. The desk stays empty until you do.

Optional: `cp .env.example .env` and add `ELISA_LLM_KEY` if you want Mistral chat. Local Ollama models work on the Anonymise page if Ollama is running.
