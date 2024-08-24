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
RUN export ZMK_MODULES=$(yq -r '.manifest.projects[].name | select(. != "zmk")' /glove80/config/west.yml)
RUN west build -s zmk/app -b glove80_lh -p=always -- -DZMK_CONFIG=/glove80/config -DZMK_EXTRA_MODULES="$ZMK_MODULES"
RUN cp build/zephyr/zmk.uf2 glove80_lh.uf2
RUN west build -s zmk/app -b glove80_rh -p=always -- -DZMK_CONFIG=/glove80/config -DZMK_EXTRA_MODULES="$ZMK_MODULES"
RUN cp build/zephyr/zmk.uf2 glove80_rh.uf2

FROM scratch AS artifact
COPY --from=build /glove80/*.uf2 .

FROM build AS release
