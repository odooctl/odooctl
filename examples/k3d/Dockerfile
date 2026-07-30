FROM odoo:19

USER root
COPY addons/ /mnt/extra-addons/
RUN chown -R odoo:odoo /mnt/extra-addons
USER odoo
