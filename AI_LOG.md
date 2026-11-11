I have checked the task then I use AI to better explanation
second step was to write a design part i decided the technologies i will use then to ask AI for doing it it gave me result i read it i Have checked everything, every step it wrote and i liked it
third step i am gonna do asking ai to prepare me db like creating tabels make a connection and write dokcer file to create images and then run the containers to check if everything works correctly
then i write this command docker compose up --build and it creates image then run it and workers 
then AI asked me to open second terminal to check workers i had 8 workers then i t gave me the script to create a job and works perfectly only one worker got it and i saw code 201 then i wrote that script again and this time it gave me code 200
then i checked effects docker compose exec db psql -U soup -c "select count(*) from effects" with this one  it gave me only  

""" 
count 
-------
     1
(1 row)
"""     even i called this twice it proves idempotency


then AI wrote chaos/run.py. I ran it with N jobs. Results are in RUNS/

## 2026-11-10: Chaos harness (chaos/run.py)
- AI wrote the harness. I ran it three times (RUNS/chaos-01..03).
- What I checked: read the stats rather than trusting the PASS lines. The run itself printed a warning that no job was retried.
- AI mistakes I caught:
  1. AI said workers would restart automatically after `docker kill` (restart: always). In all my runs the harness had to start them. <your manual check result>
  2. AI's canceller hid the retry path: crashed jobs were cancelled instead of retried. I found this by comparing "kills=72" with "retried=7".
  3. AI's log counts leaked lines from earlier runs, and its kill interval was ~4.7s, not 2s.
- AI then patched the harness (cancel targeting, scheduled kills, per-run logs). I re-ran after the patch (see runs below).

## 2026-11-10: Zombie test (chaos/zombie_test.py)
- AI wrote it. Also changed worker.py so the freeze hook applies only to attempt 1 (otherwise the retry would freeze too).
- Result: <fill in after you run it>

- AI claimed its canceller change fixed the cancel/retry interference. Run 04 showed it did not for short runs; I noticed from "retried: 0" and the cancelled count rising after the queue was empty.
- AI's harness only warned about running 7 workers instead of 8; I noticed from the container list in the run headers.

- AI suggested the invariants and stats; I read the stats rather than only the PASS lines. I noticed that 7-worker runs (04-06) were invalid and re-ran 05/06 as 08/09 with 8 workers.
- I could not independently verify why Docker did not restart killed containers. I confirmed by hand that the container stayed Exited (137).

- AI wrote chaos/mutate.py and predicted the outcome of each of the 5 breakages. I ran all 5 myself; all matched the predictions. That does not prove the tests catch faults the AI did not think of.
- One thing I found only after running: plain_claim passed all 12 original invariants (fencing hid the double claims). I added no_double_claim_without_kills and no_fenced_writes because of it.


- AI wrote chaos/heartbeat_test.py and checked its logic against a simulated server before I ran it; that found a flaw (a re-claimed job's new lease was counted as a lease extension), fixed before my runs. I ran the baseline and both mutations on the real system: results matched what the AI predicted.