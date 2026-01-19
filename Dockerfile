FROM zmkfirmware/zmk-build-arm:stable AS build
RUN apt update
RUN apt install software-properties-common -y
RUN add-apt-repository ppa:rmescandon/yq
# TODO: update not necessary?
# RUN apt-get update
RUN apt install yq -y

WORKDIR /glove80

COPY ./config/west.yml /glove80/config/west.yml
RUN west init -l /glove80/config
RUN west update
RUN west zephyr-export

COPY config /glove80/config

# Build Glove80 firmware
RUN west build -s zmk/app -b glove80_lh -p=always -S studio-rpc-usb-uart -- -DZMK_CONFIG=/glove80/config -DCONFIG_ZMK_STUDIO=y -DCONFIG_ZMK_STUDIO_TRANSPORT_BLE=y
RUN cp build/zephyr/zmk.uf2 glove80_lh.uf2
RUN west build -s zmk/app -b glove80_rh -p=always -- -DZMK_CONFIG=/glove80/config
RUN cp build/zephyr/zmk.uf2 glove80_rh.uf2

# Build Toucan firmware
RUN west build -s zmk/app -b seeeduino_xiao_ble -p=always -S studio-rpc-usb-uart -- -DZMK_CONFIG=/glove80/config -DSHIELD="toucan_left rgbled_adapter nice_view_gem" -DCONFIG_ZMK_STUDIO=y -DCONFIG_ZMK_STUDIO_TRANSPORT_BLE=y
RUN cp build/zephyr/zmk.uf2 toucan_lh.uf2
RUN west build -s zmk/app -b seeeduino_xiao_ble -p=always -- -DZMK_CONFIG=/glove80/config -DSHIELD="toucan_right rgbled_adapter"
RUN cp build/zephyr/zmk.uf2 toucan_rh.uf2

FROM scratch AS artifact
COPY --from=build /glove80/*.uf2 .

FROM build AS release
