{% macro generate_schema_name(custom_schema_name, node) -%}
    {#-
      dbt's default is to concatenate the profile schema with the custom one
      (main_staging). That produces generated names nobody asked for and hides
      the layering. Here the folder decides the schema, verbatim:
      staging / intermediate / marts / reporting / analytics / seeds.
    -#}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
