.MAIN: all

# Pi USB share server for remote flashing
PI_HOST ?= piledriver.local
FLASH_MOUNT := /tmp/kbflash

# =============================================================================
# Keyboard Configurations
# =============================================================================

# Glove80 (unique drive names per half)
GLOVE80_DRIVE_LH := GLV80LHBOOT
GLOVE80_DRIVE_RH := GLV80RHBOOT
GLOVE80_FILE_LH  := build/glove80_lh.uf2
GLOVE80_FILE_RH  := build/glove80_rh.uf2
GLOVE80_PROMPT_LH := Plug LEFT half into bootloader mode (Magic + Esc)
GLOVE80_PROMPT_RH := Plug RIGHT half into bootloader mode (Magic + apostrophe)

# Toucan (shared drive name - flash left first, then right)
TOUCAN_DRIVE_LH := XIAO-BOOT
TOUCAN_DRIVE_RH := XIAO-BOOT
TOUCAN_FILE_LH  := build/toucan_lh.uf2
TOUCAN_FILE_RH  := build/toucan_rh.uf2
TOUCAN_PROMPT_LH := Plug LEFT half into bootloader mode
TOUCAN_PROMPT_RH := Plug RIGHT half into bootloader mode

# =============================================================================
# Reusable Flash Macros
# =============================================================================

# Local flash macro: $(1)=drive, $(2)=file, $(3)=prompt
define flash-half
	@while ! (mount | grep -q $(1)); do \
		echo '$(3)...'; \
		sleep .5; \
	done
	@echo "Found $(1) - copying firmware..."
	@cp $(2) /Volumes/$(1)/
	@echo "Done!"
endef

# Remote flash macro: $(1)=drive, $(2)=file, $(3)=prompt
define flash-half-remote
	@while ! smbutil view //guest@$(PI_HOST) 2>/dev/null | grep -q "$(1)"; do \
		echo '$(3)...'; \
		sleep 1; \
	done
	@echo "Found $(1) - mounting..."
	@# Unmount any existing mounts of this share (Finder may have auto-mounted it)
	@diskutil unmount /Volumes/$(1) 2>/dev/null || true
	@umount $(FLASH_MOUNT) 2>/dev/null || diskutil unmount $(FLASH_MOUNT) 2>/dev/null || true
	@rm -rf $(FLASH_MOUNT)
	@mkdir -p $(FLASH_MOUNT)
	@mount_smbfs -N //guest@$(PI_HOST)/$(1) $(FLASH_MOUNT)
	@echo "Copying firmware..."
	@cp $(2) $(FLASH_MOUNT)/
	@sync
	@sleep 1
	@umount $(FLASH_MOUNT) 2>/dev/null || diskutil unmount $(FLASH_MOUNT) 2>/dev/null || true
	@echo "Done!"
endef

# =============================================================================
# Main Targets
# =============================================================================

all: build glove80.svg toucan.svg

# Glove80 keymap visualization
keymap.yaml: config/glove80.keymap
	keymap parse -c 10 -z config/glove80.keymap > keymap.yaml

glove80.svg: keymap.yaml
	keymap draw keymap.yaml > glove80.svg

# Toucan keymap visualization
toucan-keymap.yaml: config/toucan.keymap
	keymap parse -c 12 -z config/toucan.keymap > toucan-keymap.yaml

toucan.svg: toucan-keymap.yaml toucan-layout.json
	keymap draw -j toucan-layout.json toucan-keymap.yaml > toucan.svg

build: config Dockerfile
	docker build --progress plain --target=artifact --output type=local,dest=$$(pwd)/build/ .

# =============================================================================
# Glove80 Flash Targets
# =============================================================================

flash-glove80: build
	@echo "Flashing Glove80..."
	$(call flash-half,$(GLOVE80_DRIVE_RH),$(GLOVE80_FILE_RH),$(GLOVE80_PROMPT_RH))
	$(call flash-half,$(GLOVE80_DRIVE_LH),$(GLOVE80_FILE_LH),$(GLOVE80_PROMPT_LH))
	@echo ""
	@echo "Flash complete! Both halves updated."

flash-remote-glove80: build
	@echo "Flashing Glove80 via $(PI_HOST)..."
	@mkdir -p $(FLASH_MOUNT)
	$(call flash-half-remote,$(GLOVE80_DRIVE_RH),$(GLOVE80_FILE_RH),$(GLOVE80_PROMPT_RH))
	$(call flash-half-remote,$(GLOVE80_DRIVE_LH),$(GLOVE80_FILE_LH),$(GLOVE80_PROMPT_LH))
	@rmdir $(FLASH_MOUNT) 2>/dev/null || true
	@echo ""
	@echo "Flash complete! Both halves updated."

# =============================================================================
# Toucan Flash Targets
# =============================================================================

flash-toucan: build
	@echo "Flashing Toucan (left first, then right)..."
	$(call flash-half,$(TOUCAN_DRIVE_LH),$(TOUCAN_FILE_LH),$(TOUCAN_PROMPT_LH))
	$(call flash-half,$(TOUCAN_DRIVE_RH),$(TOUCAN_FILE_RH),$(TOUCAN_PROMPT_RH))
	@echo ""
	@echo "Flash complete! Both halves updated."

flash-remote-toucan: build
	@echo "Flashing Toucan via $(PI_HOST) (left first, then right)..."
	@mkdir -p $(FLASH_MOUNT)
	$(call flash-half-remote,$(TOUCAN_DRIVE_LH),$(TOUCAN_FILE_LH),$(TOUCAN_PROMPT_LH))
	$(call flash-half-remote,$(TOUCAN_DRIVE_RH),$(TOUCAN_FILE_RH),$(TOUCAN_PROMPT_RH))
	@rmdir $(FLASH_MOUNT) 2>/dev/null || true
	@echo ""
	@echo "Flash complete! Both halves updated."

# =============================================================================
# Default Aliases (Glove80)
# =============================================================================

flash: flash-glove80
flash-remote: flash-remote-glove80

# =============================================================================
# ZMK Studio runtime push/pull (Toucan only for now)
# =============================================================================
#
# These targets use the scripts/zmk_studio_push Python package to update
# the live keymap on the Toucan over USB CDC or BLE GATT, without rebuilding
# and reflashing firmware. Design doc: docs/superpowers/specs/2026-04-08-*
#
# Prerequisites:
#   - Toucan LH flashed with the current build.yaml (Studio enabled)
#   - Python 3.10+ (the venv auto-installs pyserial, bleak, protobuf)
#
# Usage:
#   make push-toucan                  # auto-detect USB then BLE, push source
#   make push-toucan TRANSPORT=ble    # force BLE
#   make push-toucan DRY_RUN=1        # print diff without applying
#   make pull-toucan                  # snapshot current keymap to backups/
#   make restore-toucan BACKUP=backups/toucan-<ts>.json
#   make test-zmk-studio-push         # run the package's unit tests

.venv/.installed: scripts/requirements.txt
	python3 -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -r scripts/requirements.txt
	@touch .venv/.installed

ZMK_PUSH_PY := PYTHONPATH=scripts .venv/bin/python -m zmk_studio_push

push-toucan: .venv/.installed
	$(ZMK_PUSH_PY) push \
		--keymap config/toucan.keymap \
		--backup-dir backups \
		--transport $${TRANSPORT:-auto} \
		$${DRY_RUN:+--dry-run} \
		$${FORCE:+--force}

pull-toucan: .venv/.installed
	$(ZMK_PUSH_PY) pull \
		--output-dir backups \
		--transport $${TRANSPORT:-auto}

restore-toucan: .venv/.installed
	@test -n "$(BACKUP)" || { echo "Usage: make restore-toucan BACKUP=backups/<file>.json"; exit 1; }
	$(ZMK_PUSH_PY) restore \
		--backup "$(BACKUP)" \
		--backup-dir backups \
		--transport $${TRANSPORT:-auto} \
		$${DRY_RUN:+--dry-run}

push-toucan-usb: TRANSPORT := usb
push-toucan-usb: push-toucan

push-toucan-ble: TRANSPORT := ble
push-toucan-ble: push-toucan

pull-toucan-usb: TRANSPORT := usb
pull-toucan-usb: pull-toucan

pull-toucan-ble: TRANSPORT := ble
pull-toucan-ble: pull-toucan

test-zmk-studio-push: .venv/.installed
	PYTHONPATH=scripts .venv/bin/python -m pytest scripts/zmk_studio_push/tests/ -v

.PHONY: push-toucan pull-toucan restore-toucan \
        push-toucan-usb push-toucan-ble \
        pull-toucan-usb pull-toucan-ble \
        test-zmk-studio-push
