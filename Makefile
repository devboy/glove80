keymap.yaml: config
	keymap parse -c 10 -z config/glove80.keymap > keymap.yaml

glove80.svg: keymap.yaml
	keymap draw keymap.yaml > glove80.svg

build: config Dockerfile
	docker build --progress plain --target=artifact --output type=local,dest=$$(pwd)/build/ .
