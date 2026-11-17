.PHONY: demo run test serve ledger

demo:
	@python -m selregress demo; code=$$?; echo "selregress demo exit code: $$code"; test $$code -eq 0 -o $$code -eq 2

run:
	python -m selregress run --config configs/demo.yaml

test:
	pytest -q

serve:
	python -m selregress serve --config configs/demo.yaml --port 5000

ledger:
	python -m selregress ledger --latest
