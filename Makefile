.MAIN: all

all: build glove80.svg

keymap.yaml: config
	keymap parse -c 10 -z config/glove80.keymap > keymap.yaml

glove80.svg: keymap.yaml
	keymap draw keymap.yaml > glove80.svg

build: config Dockerfile
	docker build --progress plain --target=artifact --output type=local,dest=$$(pwd)/build/ .

flash: build
	while ! (mount | grep GLV80RHBOOT); do \
		echo 'Waiting for right half to be mounted...'; \
		sleep .5; \
		done
	echo "Copying right half file"
	cp build/glove80_rh.uf2 /Volumes/GLV80RHBOOT
	while ! mount | grep GLV80LHBOOT; do \
		echo 'Waiting for left half to be mounted...'; \
		sleep .5; \
		done
	echo "Copying left half file"
	cp build/glove80_lh.uf2 /Volumes/GLV80LHBOOT
