# kotlinx.serialization ships its own rules. Keep the app's serialisable models' names, because
# the server, the drive report and older saved files depend on them.
-keep,includedescriptorclasses class nl.slot17.**$$serializer { *; }
-keepclassmembers class nl.slot17.** { *** Companion; }
-keepclasseswithmembers class nl.slot17.** { kotlinx.serialization.KSerializer serializer(...); }
-keepattributes *Annotation*, InnerClasses
