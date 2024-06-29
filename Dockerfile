FROM zmkfirmware/zmk-build-arm:stable as build

WORKDIR /glove80

COPY ./config/west.yml /glove80/config/west.yml
RUN west init -l /glove80/config
RUN west update
RUN west zephyr-export

COPY config /glove80/config
RUN west build -s zmk/app -b glove80_lh -p=always -- -DZMK_CONFIG=/glove80/config
RUN cp build/zephyr/zmk.uf2 glove80_lh.uf2
RUN west build -s zmk/app -b glove80_rh -p=always -- -DZMK_CONFIG=/glove80/config
RUN cp build/zephyr/zmk.uf2 glove80_rh.uf2

FROM scratch as artifact
COPY --from=build /glove80/*.uf2 .

FROM build as release
